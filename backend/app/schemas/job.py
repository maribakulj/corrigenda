"""Server-side job enums and record (moved out of corrigenda by spec F12).

``Provider``, ``JobStatus`` and ``JobManifest`` (with its ``images`` map)
are backend concerns — the pure correction core does not enumerate LLM
vendors or track a server job's lifecycle. They live here now; corrigenda
keeps only the domain enums (``LineStatus``, ``ChunkGranularity``,
``HyphenRole``, ``PipelineEventType``).
"""

from __future__ import annotations

from enum import Enum

from corrigenda.core.schemas import CorrectionReport, DocumentManifest
from pydantic import BaseModel, ConfigDict, Field


class JobStatus(str, Enum):
    """Lifecycle state of a correction job, surfaced to API clients."""

    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    #: P0-1 — terminal success where one or more lines fell back to their
    #: OCR source text (rejected LLM output, repeated per-chunk failures).
    #: The corrected files are valid and downloadable, but the run is
    #: explicitly DEGRADED: consumers must be able to distinguish "every
    #: line went through the provider" from "some lines silently kept
    #: their OCR text". COMPLETED now strictly means zero fallbacks.
    COMPLETED_WITH_FALLBACKS = "completed_with_fallbacks"
    FAILED = "failed"
    #: Plan V2.2 — cooperative cancellation. CANCEL_REQUESTED is set by
    #: the cancel endpoint; the pipeline's `should_abort` probe trips
    #: between chunks/pages and the runner lands the job in CANCELLED
    #: (terminal — no output is ever promoted).
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


#: The two terminal states whose outputs are valid and downloadable.
TERMINAL_SUCCESS_STATES = frozenset({JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_FALLBACKS})


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
    # P1-7 — SHA-256 hex of the capability token required to access this
    # job's endpoints. None = legacy/direct-store job (no enforcement) —
    # every job created through the public API carries one.
    token_hash: str | None = None
    status: JobStatus = JobStatus.QUEUED
    document_manifest: DocumentManifest | None = None
    total_lines: int = 0
    lines_modified: int = 0
    chunks_total: int = 0
    retries: int = 0
    #: Number of LINES that kept their OCR source text (chunk fallback,
    #: guard rejection or duplicate revert) — the UI renders this as
    #: "N line(s) fell back", so it must never be a chunk count.
    fallbacks: int = 0
    duration_seconds: float | None = None
    error: str | None = None
    images: dict[str, str] = Field(default_factory=dict)
    # §9 — the run's public CorrectionReport: the per-line LineTrace list,
    # promoted to the versioned artefact. The /trace endpoint serves this;
    # trace.json on disk is its JSON dump. This is the ONLY trace copy the
    # job keeps (the former parallel ``line_traces`` dict was redundant —
    # nothing read it — and is gone).
    report: CorrectionReport | None = None


__all__ = ["TERMINAL_SUCCESS_STATES", "JobManifest", "JobStatus", "Provider"]
