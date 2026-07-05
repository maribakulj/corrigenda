"""Server-side job enums and record (moved out of alto-core by spec F12).

``Provider``, ``JobStatus`` and ``JobManifest`` (with its ``images`` map)
are backend concerns — the pure correction core does not enumerate LLM
vendors or track a server job's lifecycle. They live here now; alto-core
keeps only the domain enums (``LineStatus``, ``ChunkGranularity``,
``HyphenRole``, ``PipelineEventType``).
"""

from __future__ import annotations

from enum import Enum

from alto_core.schemas import DocumentManifest, LineTrace
from pydantic import BaseModel, ConfigDict, Field


class JobStatus(str, Enum):
    """Lifecycle state of a correction job, surfaced to API clients."""

    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Provider(str, Enum):
    """Identifier for an LLM vendor. Each value maps to one ``BaseProvider``."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    MISTRAL = "mistral"
    GOOGLE = "google"


class JobManifest(BaseModel):
    """Server-side record of a correction job — status, counters, trace data."""

    # L10/F6 — `JobStore.update_job` mutates fields via `setattr(job, k, v)`
    # in a loop. Pydantic v2's default `validate_assignment=False` would
    # silently accept any type at assignment time, so a typo like
    # `update_job(jid, status="garbage")` lands a string into the enum
    # field; downstream `job.status.value` then crashes far from the
    # original mistake. Turning validation on at assignment surfaces
    # the bug at the offending call-site immediately.
    model_config = ConfigDict(validate_assignment=True)

    job_id: str
    provider: Provider
    model: str
    status: JobStatus = JobStatus.QUEUED
    document_manifest: DocumentManifest | None = None
    total_lines: int = 0
    lines_modified: int = 0
    chunks_total: int = 0
    retries: int = 0
    fallbacks: int = 0
    duration_seconds: float | None = None
    error: str | None = None
    images: dict[str, str] = Field(default_factory=dict)
    # Per-line text trace through every pipeline stage. Keyed by
    # f"{page_id}:{line_order_global}:{line_id}" (see _trace_key in
    # alto_core.pipeline.correction_pipeline).
    line_traces: dict[str, LineTrace] = Field(default_factory=dict)


__all__ = ["JobManifest", "JobStatus", "Provider"]
