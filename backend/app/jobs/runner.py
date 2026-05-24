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
import time
from pathlib import Path
from typing import Any

from app.jobs.correction_pipeline import CorrectionPipeline, sanitize_error
from app.protocols import BaseProvider, JobStore
from app.schemas import DocumentManifest, JobStatus
from app.storage.output_writer import FilesystemOutputWriter

logger = logging.getLogger(__name__)


class JobStoreObserver:
    """Adapt a JobStore to the PipelineObserver Protocol for a single job."""

    def __init__(self, job_store: JobStore, job_id: str) -> None:
        self._job_store = job_store
        self._job_id = job_id

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._job_store.emit(self._job_id, event_type, payload)


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
        output_dir: Path,
        source_files: dict[str, Path],
        provider: BaseProvider | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        """Run a job end-to-end. Updates the JobStore as side effect.

        `source_files`: mapping of source_name → xml_path on disk.
        `provider`: injected provider (for testing); if None, resolved
        from the global registry via `app.providers.get_provider`.
        `timeout_seconds`: 0 disables the timeout.
        """
        if provider is None:
            from app.providers import get_provider
            from app.schemas import Provider

            provider = get_provider(Provider(provider_name))

        output_writer = FilesystemOutputWriter(output_dir)
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
                "completed",
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
            self.job_store.emit(job_id, "failed", {"job_id": job_id, "error": safe_error})

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
                "failed",
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
        output_writer: FilesystemOutputWriter,
        source_files: dict[str, Path],
    ) -> tuple[int, int]:
        """Drive the pure pipeline and persist its counters back."""
        self.job_store.update_job(job_id, status=JobStatus.STARTED)
        self.job_store.emit(job_id, "started", {"job_id": job_id})

        self.job_store.update_job(
            job_id,
            status=JobStatus.RUNNING,
            document_manifest=document_manifest,
            total_lines=document_manifest.total_lines,
        )

        pipeline = CorrectionPipeline(
            provider=provider,
            observer=JobStoreObserver(self.job_store, job_id),
            output_writer=output_writer,
        )
        result = await pipeline.run(
            job_id=job_id,
            document_manifest=document_manifest,
            api_key=api_key,
            model=model,
            provider_name=provider_name,
            source_files=source_files,
        )

        self.job_store.update_job(
            job_id,
            retries=result.retry_count,
            fallbacks=result.fallback_count,
            line_traces=result.traces,
        )

        return result.total_chunks, result.total_reconciled
