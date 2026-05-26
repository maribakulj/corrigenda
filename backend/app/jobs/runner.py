"""JobRunner — bridges the pure `CorrectionPipeline` with infrastructure.

Owns the job lifecycle (STARTED → RUNNING → COMPLETED/FAILED), the
timeout budget, the observer adapter, and error sanitisation. Stays
agnostic of how the JobStore is wired in: callers pass it at
construction time, which is the seam future-1.4 work will use to
replace the in-memory singleton with `request.app.state.job_store`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import warnings
from pathlib import Path

from alto_core import CorrectionPipeline, sanitize_error
from alto_core.schemas import PipelineEventType

from app.jobs.observers import CompositeObserver, JobStoreObserver, LoggingObserver
from app.protocols import BaseProvider, JobStore, OutputWriter
from app.schemas import DocumentManifest, JobStatus

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
    ) -> None:
        """Run a job end-to-end. Updates the JobStore as side effect.

        `output_writer`: injected sink for the corrected ALTO + trace.
        The caller chooses the implementation (filesystem, S3, in-memory
        for tests, ...) — the runner stays oblivious of where outputs land.
        `source_files`: mapping of source_name → xml_path on disk.
        `provider`: injected provider (for testing); if None, resolved
        from the global registry via `app.providers.get_provider`.
        `timeout_seconds`: 0 disables the timeout.
        """
        if provider is None:
            from app.providers import get_provider
            from app.schemas import Provider

            provider = get_provider(Provider(provider_name))

        start_time = time.monotonic()

        try:
            timeout = timeout_seconds if timeout_seconds > 0 else None
            total_chunks, total_reconciled = await asyncio.wait_for(
                self._run_pipeline(
                    job_id=job_id,
                    document_manifest=document_manifest,
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    provider_name=provider_name,
                    output_writer=output_writer,
                    source_files=source_files,
                ),
                timeout=timeout,
            )

            lines_modified = sum(
                1
                for page in document_manifest.pages
                for lm in page.lines
                if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
            )
            elapsed = round(time.monotonic() - start_time, 2)

            self.job_store.update_job(
                job_id,
                status=JobStatus.COMPLETED,
                chunks_total=total_chunks,
                lines_modified=lines_modified,
                duration_seconds=elapsed,
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
                },
            )

        except TimeoutError:
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

        except asyncio.CancelledError:
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

        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            # Sanitise BEFORE truncating: if the api_key straddles the 500-char
            # boundary, slicing first would leave half the key visible and the
            # regex would fail to mask it.
            safe_error = sanitize_error(str(exc), api_key)[:500]
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=time.monotonic() - start_time,
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
    ) -> tuple[int, int]:
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
        # standard logger (for operators). ADR-006: alto-core never
        # logs by itself — adapters here own the routing.
        pipeline = CorrectionPipeline(
            provider=provider,
            observer=CompositeObserver(
                [JobStoreObserver(self.job_store, job_id), LoggingObserver()]
            ),
            output_writer=output_writer,
        )
        # `run_id` is alto-core's generic identifier; we feed it the
        # server-side `job_id` so trace.json correlates with the API.
        result = await pipeline.run(
            document_manifest=document_manifest,
            api_key=api_key,
            model=model,
            provider_name=provider_name,
            source_files=source_files,
            run_id=job_id,
        )

        self.job_store.update_job(
            job_id,
            retries=result.retry_count,
            fallbacks=result.fallback_count,
            line_traces=result.traces,
        )

        return result.total_chunks, result.total_reconciled
