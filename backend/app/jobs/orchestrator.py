"""Job orchestration — wires infrastructure (JobStore, filesystem) around
the pure `CorrectionPipeline`.

The pipeline itself lives in `app.jobs.correction_pipeline` and depends
only on Protocols. This module owns the job lifecycle (status transitions,
counter persistence, timeout, error sanitisation) and adapts `job_store`
to the `PipelineObserver` Protocol via `_JobStoreObserver`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from app.jobs.correction_pipeline import CorrectionPipeline, sanitize_error
from app.jobs.store import job_store
from app.protocols import BaseProvider
from app.schemas import DocumentManifest, JobStatus
from app.storage.output_writer import FilesystemOutputWriter

logger = logging.getLogger(__name__)

# Global timeout for the entire job pipeline (seconds). 0 = no limit.
try:
    _JOB_TIMEOUT_SECONDS: int = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
except ValueError:
    import warnings as _warnings
    _warnings.warn(
        "JOB_TIMEOUT_SECONDS env var is not a valid integer; using default 1800s",
        stacklevel=1,
    )
    _JOB_TIMEOUT_SECONDS = 1800


class _JobStoreObserver:
    """Adapt the in-memory JobStore to the PipelineObserver Protocol.

    `job_store` is looked up at call time (not closed over) so that
    test substitution (`orch_module.job_store = store`) is honoured.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        job_store.emit(self.job_id, event_type, payload)


async def _run_pipeline(
    job_id: str,
    document_manifest: DocumentManifest,
    provider: BaseProvider,
    api_key: str,
    model: str,
    provider_name: str,
    output_writer: FilesystemOutputWriter,
    source_files: dict[str, Path],
) -> tuple[int, int]:
    """Drive the pure pipeline and persist its result back to the job store."""
    job_store.update_job(job_id, status=JobStatus.STARTED)
    job_store.emit(job_id, "started", {"job_id": job_id})

    job_store.update_job(
        job_id,
        status=JobStatus.RUNNING,
        document_manifest=document_manifest,
        total_lines=document_manifest.total_lines,
    )

    pipeline = CorrectionPipeline(
        provider=provider,
        observer=_JobStoreObserver(job_id),
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

    job_store.update_job(
        job_id,
        retries=result.retry_count,
        fallbacks=result.fallback_count,
        line_traces=result.traces,
    )

    return result.total_chunks, result.total_reconciled


async def run_job(
    job_id: str,
    document_manifest: DocumentManifest,
    provider_name: str,
    api_key: str,
    model: str,
    output_dir: Path,
    source_files: dict[str, Path],
    provider: Optional[BaseProvider] = None,
) -> None:
    """Run the full correction pipeline for a job.

    `source_files`: mapping of source_name → xml_path on disk.
    `provider`: injected provider (for testing); if None, resolved from registry.
    """
    if provider is None:
        from app.providers import get_provider
        from app.schemas import Provider
        provider = get_provider(Provider(provider_name))

    output_writer = FilesystemOutputWriter(output_dir)
    start_time = time.monotonic()

    try:
        timeout = _JOB_TIMEOUT_SECONDS if _JOB_TIMEOUT_SECONDS > 0 else None
        total_chunks, total_reconciled = await asyncio.wait_for(
            _run_pipeline(
                job_id, document_manifest, provider, api_key, model,
                provider_name, output_writer, source_files,
            ),
            timeout=timeout,
        )

        lines_modified = sum(
            1 for page in document_manifest.pages
            for lm in page.lines
            if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
        )
        elapsed = round(time.monotonic() - start_time, 2)

        job_store.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            chunks_total=total_chunks,
            lines_modified=lines_modified,
            duration_seconds=elapsed,
        )

        job_store.emit(job_id, "completed", {
            "job_id": job_id,
            "total_lines": document_manifest.total_lines,
            "lines_modified": lines_modified,
            "hyphen_pairs_total": total_reconciled,
            "chunks_total": total_chunks,
            "duration_seconds": elapsed,
        })

    except asyncio.TimeoutError:
        logger.error("Job %s timed out after %ss", job_id, _JOB_TIMEOUT_SECONDS)
        elapsed = round(time.monotonic() - start_time, 2)
        safe_error = f"Job timed out after {_JOB_TIMEOUT_SECONDS}s"
        job_store.update_job(
            job_id, status=JobStatus.FAILED, error=safe_error,
            duration_seconds=elapsed,
        )
        job_store.emit(job_id, "failed", {"job_id": job_id, "error": safe_error})

    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        # Sanitize BEFORE truncating: if the api_key straddles the 500-char
        # boundary, slicing first would leave half the key visible and the
        # regex would fail to mask it.
        safe_error = sanitize_error(str(exc), api_key)[:500]
        job_store.update_job(
            job_id,
            status=JobStatus.FAILED,
            error=safe_error,
            duration_seconds=time.monotonic() - start_time,
        )
        job_store.emit(job_id, "failed", {
            "job_id": job_id,
            "error": safe_error,
        })
