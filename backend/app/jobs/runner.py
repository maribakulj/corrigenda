"""JobRunner — bridges the pure ``CorrectionPipeline`` with infrastructure.

Owns the job lifecycle (STARTED → RUNNING → COMPLETED/FAILED), the
timeout budget, the observer adapter, and error sanitisation. The
JobStore is injected at construction time so it can be swapped (in
tests, or for a future out-of-process store) without touching the
pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import warnings
from collections.abc import Callable
from pathlib import Path

from corrigenda import CorrectionAborted, CorrectionPipeline, CorrectionResult, sanitize_error
from corrigenda.core.protocols import ProviderPermanentError

from app.jobs.observers import CompositeObserver, JobStoreObserver, LoggingObserver
from app.protocols import BaseProvider, JobStore, OutputWriter
from app.schemas import DocumentManifest, JobStatus, PipelineEventType

logger = logging.getLogger(__name__)


def _default_timeout_from_env() -> int:
    """Resolve JOB_TIMEOUT_SECONDS once at import. 0 disables the timeout.

    Kept at module scope (rather than inside ``JobRunner.__init__``) so
    tests can ``monkeypatch.setattr("app.jobs.runner.DEFAULT_JOB_TIMEOUT_SECONDS", N)``
    when they need a tighter budget than the production default.
    """
    try:
        return int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
    except ValueError:
        warnings.warn(
            "JOB_TIMEOUT_SECONDS env var is not a valid integer; using default 1800s",
            stacklevel=1,
        )
        return 1800


DEFAULT_JOB_TIMEOUT_SECONDS: int = _default_timeout_from_env()


class JobRunner:
    """Drives a `CorrectionPipeline` and persists its outcome to a JobStore."""

    def __init__(self, job_store: JobStore) -> None:
        self.job_store = job_store

    @staticmethod
    def _commit_outputs(output_writer: OutputWriter) -> None:
        """P0-4 — promote staged outputs atomically after a successful
        run. Duck-typed: writers without a staging concept (in-memory
        test doubles, custom sinks) simply skip it."""
        commit = getattr(output_writer, "commit", None)
        if callable(commit):
            commit()

    @staticmethod
    def _discard_outputs(output_writer: OutputWriter) -> None:
        """P0-4 — drop staged outputs on failure/timeout/cancellation so
        nothing partial ever becomes downloadable."""
        discard = getattr(output_writer, "discard", None)
        if callable(discard):
            discard()

    async def run(
        self,
        *,
        job_id: str,
        document_manifest: DocumentManifest,
        provider_name: str,
        api_key: str,
        model: str,
        output_writer: OutputWriter,
        source_files: dict[str, Path],
        provider: BaseProvider | None = None,
        timeout_seconds: int = 1800,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        """Run a job end-to-end. Updates the JobStore as side effect.

        `output_writer`: injected sink for the corrected ALTO + trace.
        The caller chooses the implementation (filesystem, S3, in-memory
        for tests, ...) — the runner stays oblivious of where outputs land.
        `source_files`: mapping of source_name → xml_path on disk.
        `provider`: injected provider (for testing); if None, resolved
        from the global registry via `app.providers.get_provider`.
        `timeout_seconds`: 0 disables the timeout.
        `should_abort`: Plan V2.2 — cooperative cancellation probe,
        forwarded to the pipeline (polled between pages and chunks).
        When it trips, the job lands in CANCELLED with no output promoted.
        """
        if provider is None:
            from app.providers import get_provider
            from app.schemas import Provider

            provider = get_provider(Provider(provider_name))

        start_time = time.monotonic()

        try:
            timeout = timeout_seconds if timeout_seconds > 0 else None
            result = await asyncio.wait_for(
                self._run_pipeline(
                    job_id=job_id,
                    document_manifest=document_manifest,
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    provider_name=provider_name,
                    output_writer=output_writer,
                    source_files=source_files,
                    should_abort=should_abort,
                ),
                timeout=timeout,
            )
            total_chunks = result.total_chunks
            total_reconciled = result.total_reconciled

            lines_modified = sum(
                1
                for page in document_manifest.pages
                for lm in page.lines
                if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
            )
            elapsed = round(time.monotonic() - start_time, 2)

            # P0-4 — promote the staged outputs BEFORE the job turns
            # terminal-success: a client that sees the status can always
            # download the complete, committed set.
            self._commit_outputs(output_writer)

            # P0-1 — COMPLETED strictly means "zero fallbacks". A run where
            # some lines silently kept their OCR source text is a DEGRADED
            # success and says so in its terminal state.
            terminal = (
                JobStatus.COMPLETED_WITH_FALLBACKS
                if result.fallback_count > 0
                else JobStatus.COMPLETED
            )
            self.job_store.update_job(
                job_id,
                status=terminal,
                chunks_total=total_chunks,
                lines_modified=lines_modified,
                duration_seconds=elapsed,
            )

            # Job-end reconcile_stats observability event — emitted just
            # BEFORE the terminal `completed` so subscribers that exit
            # on `completed` still receive it.
            self.job_store.emit(
                job_id,
                PipelineEventType.RECONCILE_STATS,
                {
                    "coherent": result.reconcile_metrics.coherent,
                    "fallback": result.reconcile_metrics.fallback,
                    "neutralised": result.reconcile_metrics.neutralised,
                    "total": result.reconcile_metrics.total,
                },
            )

            self.job_store.emit(
                job_id,
                PipelineEventType.COMPLETED,
                {
                    "job_id": job_id,
                    "total_lines": document_manifest.total_lines,
                    "lines_modified": lines_modified,
                    "hyphen_pairs_total": total_reconciled,
                    "chunks_total": total_chunks,
                    "duration_seconds": elapsed,
                    # P0-1 — degraded-success visibility: the terminal
                    # status and the fallback count ride the event so the
                    # client can render "success" vs "success with N
                    # uncorrected lines" without an extra round-trip.
                    "status": terminal.value,
                    "fallbacks": result.fallback_count,
                },
            )

        except TimeoutError:
            self._discard_outputs(output_writer)
            logger.error("Job %s timed out after %ss", job_id, timeout_seconds)
            elapsed = round(time.monotonic() - start_time, 2)
            safe_error = f"Job timed out after {timeout_seconds}s"
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=elapsed,
            )
            self.job_store.emit(
                job_id, PipelineEventType.FAILED, {"job_id": job_id, "error": safe_error}
            )

        except CorrectionAborted:
            # Plan V2.2 — the user's cancel request tripped the pipeline's
            # should_abort probe. This is a REQUESTED outcome, not a
            # failure: distinct terminal state, no output promoted.
            self._discard_outputs(output_writer)
            logger.info("Job %s cancelled on user request", job_id)
            elapsed = round(time.monotonic() - start_time, 2)
            self.job_store.update_job(
                job_id,
                status=JobStatus.CANCELLED,
                duration_seconds=elapsed,
            )
            self.job_store.emit(job_id, PipelineEventType.CANCELLED, {"job_id": job_id})

        except asyncio.CancelledError:
            self._discard_outputs(output_writer)
            # L10/B8 — SIGTERM during shutdown cancels the runner task
            # via `BackgroundTaskRegistry.shutdown()` past the 30 s grace
            # deadline. `CancelledError` extends `BaseException` (not
            # `Exception`) in Python 3.8+, so without this handler it
            # slipped past both `except TimeoutError` and `except
            # Exception` — leaving the job in RUNNING forever. The job
            # would never enter `_completed_at`, never be evicted, and
            # leak across redeploys.
            logger.warning("Job %s cancelled (likely server shutdown)", job_id)
            elapsed = round(time.monotonic() - start_time, 2)
            safe_error = "Job cancelled (server shutdown or task cancellation)"
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=elapsed,
            )
            self.job_store.emit(
                job_id, PipelineEventType.FAILED, {"job_id": job_id, "error": safe_error}
            )
            # Re-raise so the task scheduler sees the cancellation and
            # propagates it correctly (this is the documented asyncio
            # pattern for handling CancelledError — never silently swallow).
            raise

        except ProviderPermanentError as exc:
            self._discard_outputs(output_writer)
            # P0-1 — the provider definitively rejected the request
            # (invalid key, unknown model, 4xx family). The message is
            # already built provider-side without credentials; sanitise
            # anyway (defence in depth) and fail with a clear, actionable
            # error instead of ever reaching COMPLETED.
            logger.error(
                "Job %s failed on a permanent provider error (HTTP %s)",
                job_id,
                exc.status_code,
            )
            elapsed = round(time.monotonic() - start_time, 2)
            safe_error = sanitize_error(str(exc), api_key)[:500]
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=elapsed,
            )
            self.job_store.emit(
                job_id,
                PipelineEventType.FAILED,
                {"job_id": job_id, "error": safe_error},
            )

        except Exception as exc:
            self._discard_outputs(output_writer)
            logger.exception("Job %s failed", job_id)
            # Sanitise BEFORE truncating: if the api_key straddles the 500-char
            # boundary, slicing first would leave half the key visible and the
            # regex would fail to mask it.
            safe_error = sanitize_error(str(exc), api_key)[:500]
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                # Audit P3 — round like the three other handlers do (was
                # the only one recording an unrounded float).
                duration_seconds=round(time.monotonic() - start_time, 2),
            )
            self.job_store.emit(
                job_id,
                PipelineEventType.FAILED,
                {
                    "job_id": job_id,
                    "error": safe_error,
                },
            )

    async def _run_pipeline(
        self,
        *,
        job_id: str,
        document_manifest: DocumentManifest,
        provider: BaseProvider,
        api_key: str,
        model: str,
        provider_name: str,
        output_writer: OutputWriter,
        source_files: dict[str, Path],
        should_abort: Callable[[], bool] | None = None,
    ) -> CorrectionResult:
        """Drive the pure pipeline and persist its counters back."""
        self.job_store.update_job(job_id, status=JobStatus.STARTED)
        self.job_store.emit(job_id, PipelineEventType.STARTED, {"job_id": job_id})

        self.job_store.update_job(
            job_id,
            status=JobStatus.RUNNING,
            document_manifest=document_manifest,
            total_lines=document_manifest.total_lines,
        )

        # Fan events out to the job store (for SSE clients) and to the
        # standard logger (for operators). ADR-006: corrigenda never
        # logs by itself — adapters here own the routing.
        #
        # §5.1 resorption — credentials go into the producer (via the
        # for_provider convenience), never into run(): the pipeline surface
        # carries no api_key anywhere.
        pipeline = CorrectionPipeline.for_provider(
            provider,
            api_key=api_key,
            model=model,
            provider_name=provider_name,
            observer=CompositeObserver(
                [JobStoreObserver(self.job_store, job_id), LoggingObserver()]
            ),
            output_writer=output_writer,
        )
        # `run_id` is corrigenda's generic identifier; we feed it the
        # server-side `job_id` so trace.json correlates with the API.
        result = await pipeline.run(
            document_manifest=document_manifest,
            source_files=source_files,
            run_id=job_id,
            # Plan V2.2 — the cancel endpoint's event, polled by the
            # pipeline between pages and chunks.
            should_abort=should_abort,
        )

        self.job_store.update_job(
            job_id,
            retries=result.retry_count,
            fallbacks=result.fallback_count,
            # §9 unification — the run's CorrectionReport is the job's trace
            # artefact (served by /trace, dumped as trace.json). run_id ==
            # job_id (fed above), so the report self-correlates with the API.
            # It carries the per-line LineTrace list; no separate copy is kept.
            report=result.report,
        )

        return result
