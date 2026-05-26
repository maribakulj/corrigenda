"""Backward-compatible `run_job` entry point.

The real work lives in `app.jobs.runner.JobRunner`. This module exists
to keep the original `run_job(...)` callable available for existing
callers and tests that haven't migrated to instantiating a `JobRunner`
directly. The function now requires `job_store` to be passed
explicitly — there is no longer a module-level singleton to fall back on.

`_JOB_TIMEOUT_SECONDS` is kept at module scope so that tests can
monkey-patch it to shorten the budget.
"""

from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path

from app.jobs.runner import JobRunner
from app.protocols import BaseProvider, JobStore
from app.schemas import DocumentManifest
from app.storage.output_writer import FilesystemOutputWriter

logger = logging.getLogger(__name__)

# Global timeout for the entire job pipeline (seconds). 0 = no limit.
# Kept at module scope so tests can substitute it via attribute write.
try:
    _JOB_TIMEOUT_SECONDS: int = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
except ValueError:
    warnings.warn(
        "JOB_TIMEOUT_SECONDS env var is not a valid integer; using default 1800s",
        stacklevel=1,
    )
    _JOB_TIMEOUT_SECONDS = 1800


async def run_job(
    job_id: str,
    document_manifest: DocumentManifest,
    provider_name: str,
    api_key: str,
    model: str,
    output_dir: Path,
    source_files: dict[str, Path],
    provider: BaseProvider | None = None,
    job_store_override: JobStore | None = None,
) -> None:
    """Compat wrapper around `JobRunner.run`.

    `job_store_override` is required — there is no longer a module-level
    singleton to default to. New code should instantiate `JobRunner`
    directly; this wrapper exists for callers that haven't migrated yet.

    `_JOB_TIMEOUT_SECONDS` is looked up at the module scope so test
    patches keep working.
    """
    if job_store_override is None:
        raise ValueError(
            "run_job requires `job_store_override`. The module-level "
            "job_store singleton has been removed; pass a JobStore "
            "instance explicitly, or call JobRunner directly."
        )
    runner = JobRunner(job_store=job_store_override)
    # JobRunner takes an OutputWriter Protocol now (audit A2); construct
    # the filesystem-backed default here so the legacy `output_dir`
    # signature still works for callers that haven't migrated yet.
    await runner.run(
        job_id=job_id,
        document_manifest=document_manifest,
        provider_name=provider_name,
        api_key=api_key,
        model=model,
        output_writer=FilesystemOutputWriter(output_dir),
        source_files=source_files,
        provider=provider,
        timeout_seconds=_JOB_TIMEOUT_SECONDS,
    )
