"""Backward-compatible entry point for the correction pipeline.

The real work lives in `app.jobs.runner.JobRunner`, which is what the
API layer should be calling once `JobStore` injection is complete
(Phase 1.4). This module exists to:
  - keep the `run_job(...)` function available for existing callers
    and tests that haven't migrated yet
  - expose `job_store` and `_JOB_TIMEOUT_SECONDS` at module scope so
    legacy tests substituting them continue to work
"""
from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path
from typing import Optional

from app.jobs.runner import JobRunner
from app.jobs.store import job_store  # noqa: F401  (re-exported for test compat)
from app.protocols import BaseProvider
from app.schemas import DocumentManifest

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
    provider: Optional[BaseProvider] = None,
) -> None:
    """Compat wrapper around `JobRunner.run` — uses the module-level
    `job_store` (so test substitution still applies) and the module-level
    `_JOB_TIMEOUT_SECONDS` (so test patches still apply).
    """
    runner = JobRunner(job_store=job_store)
    await runner.run(
        job_id=job_id,
        document_manifest=document_manifest,
        provider_name=provider_name,
        api_key=api_key,
        model=model,
        output_dir=output_dir,
        source_files=source_files,
        provider=provider,
        timeout_seconds=_JOB_TIMEOUT_SECONDS,
    )
