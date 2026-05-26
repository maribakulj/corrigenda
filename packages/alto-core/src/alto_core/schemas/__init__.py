from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    """Lifecycle state of a correction job, surfaced to API clients."""

    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LineStatus(str, Enum):
    """Per-line outcome after the pipeline has visited a TextLine."""

    PENDING = "pending"
    CORRECTED = "corrected"
    FALLBACK = "fallback"
    FAILED = "failed"


class ChunkGranularity(str, Enum):
    """Granularity tier used by the chunk planner — PAGE → BLOCK → WINDOW → LINE on downgrade."""

    PAGE = "page"
    BLOCK = "block"
    WINDOW = "window"
    LINE = "line"


class Provider(str, Enum):
    """Identifier for an LLM vendor. Each enum value maps to one ``BaseProvider`` implementation."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    MISTRAL = "mistral"
    GOOGLE = "google"


class HyphenRole(str, Enum):
    """Position of a line within a hyphenated pair.

    ``NONE`` for ordinary lines. ``PART1`` is the FIRST (top) line of
    a pair — it carries the left word fragment and ends with the
    trailing hyphen. ``PART2`` is the SECOND (bottom) line of the
    pair — it carries the right word fragment. ``BOTH`` is the
    PART2-of-the-previous-pair AND PART1-of-the-next-pair (chained
    hyphenation across three consecutive lines).

    Verified against examples/sample.xml: TL4 (the line carrying the
    HYP element) is PART1; TL5 (the next line) is PART2. The previous
    docstring inverted these — a real trap for any reader trying to
    reason about the data model.
    """

    NONE = "none"
    PART1 = (
        "HypPart1"  # first (top) line of pair: carries left fragment + trailing hyphen
    )
    PART2 = "HypPart2"  # second (bottom) line of pair: carries right fragment
    BOTH = "HypBoth"  # PART2 of previous pair AND PART1 of next pair (chained)


class PipelineEventType(str, Enum):
    """Canonical event names emitted by the correction pipeline.

    This enum is the authoritative source of truth for every event
    name the pipeline or its observers can emit. The backend's SSE
    layer transports the same strings; ``frontend/src/hooks/useJobStream
    .ts::EVENTS`` lists them on the consumer side.
    Synchronisation is enforced by
    ``backend/tests/test_sse_event_contract.py`` at every CI run.

    The string values are part of the wire contract and stay stable
    across releases.
    """

    # Pipeline lifecycle (emitted by JobRunner on the backend)
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"

    # Document / page / chunk lifecycle (emitted by CorrectionPipeline)
    DOCUMENT_PARSED = "document_parsed"
    PAGE_STARTED = "page_started"
    PAGE_COMPLETED = "page_completed"
    CHUNK_PLANNED = "chunk_planned"
    CHUNK_STARTED = "chunk_started"
    CHUNK_COMPLETED = "chunk_completed"
    CHUNK_ERROR = "chunk_error"
    RETRY = "retry"
    WARNING = "warning"
    HYPHEN_PARTNER_MISSING = "hyphen_partner_missing"

    # Observability stats — emitted at file/job boundaries with rewriter
    # and reconcile path counts. Pure read-only diagnostics; never
    # influence the corrected XML output.
    REWRITER_STATS = "rewriter_stats"
    RECONCILE_STATS = "reconcile_stats"

    # Frontend-only initial state (kept here so the contract test can
    # verify the frontend list against this canonical set).
    QUEUED = "queued"

    # Transport-layer events (emitted by JobStore.stream_events on the
    # backend, not by the pipeline itself).
    KEEPALIVE = "keepalive"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class Coords(BaseModel):
    """ALTO geometry box — pixels in the source image's coordinate system."""

    hpos: int
    vpos: int
    width: int
    height: int


# ---------------------------------------------------------------------------
# Core line / block / page / document models
# ---------------------------------------------------------------------------


class LineManifest(BaseModel):
    """A single ALTO ``TextLine`` enriched with correction + hyphenation state.

    Carries the OCR text, the corrected text once the pipeline has
    visited it, the line's place in the global reading order, and any
    hyphenation links to its partner line(s). Mutated in place during
    a pipeline run; callers read ``corrected_text`` and ``status``
    once the job completes.
    """

    line_id: str
    page_id: str
    block_id: str
    line_order_global: int
    line_order_in_block: int
    coords: Coords
    ocr_text: str
    prev_line_id: str | None = None
    next_line_id: str | None = None
    corrected_text: str | None = None
    status: LineStatus = LineStatus.PENDING

    # Hyphenation fields
    # For PART1: pair_line_id = forward partner (the PART2 line)
    # For PART2: pair_line_id = backward partner (the PART1 line)
    # For BOTH:  pair_line_id = backward partner, forward_* = forward partner
    #
    # pair_page_id / forward_pair_page_id qualify the partner reference so
    # cross-page lookups stay correct when two ALTO files share TextLine IDs
    # (e.g. both call their first line "TL1"). When None, the partner is
    # presumed intra-page and the bare line_id lookup is authoritative.
    hyphen_role: HyphenRole = HyphenRole.NONE
    hyphen_pair_line_id: str | None = None
    hyphen_pair_page_id: str | None = None
    hyphen_subs_content: str | None = None
    hyphen_source_explicit: bool = False
    # Forward link fields — used only when role == BOTH (chained hyphenation)
    hyphen_forward_pair_id: str | None = None
    hyphen_forward_pair_page_id: str | None = None
    hyphen_forward_subs_content: str | None = None
    hyphen_forward_explicit: bool = False


class BlockManifest(BaseModel):
    """An ALTO ``TextBlock`` with its coordinates and the line IDs it contains."""

    block_id: str
    page_id: str
    block_order: int
    coords: Coords
    line_ids: list[str]


class PageManifest(BaseModel):
    """An ALTO ``Page``: source file, geometry, and the blocks and lines it owns."""

    page_id: str
    source_file: str
    page_index: int
    page_width: int
    page_height: int
    blocks: list[BlockManifest]
    lines: list[LineManifest]
    status: JobStatus = JobStatus.QUEUED


class DocumentManifest(BaseModel):
    """A multi-page document: the top-level structure the pipeline consumes."""

    document_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_files: list[str]
    pages: list[PageManifest]
    total_pages: int
    total_blocks: int
    total_lines: int
    status: JobStatus = JobStatus.QUEUED


# ---------------------------------------------------------------------------
# Chunk planning
# ---------------------------------------------------------------------------


class ChunkPlannerConfig(BaseModel):
    """Tunables for the chunk planner — character + line budgets per LLM request."""

    max_input_chars_per_request: int = 12000
    max_lines_per_request: int = 80
    line_window_size: int = 12
    line_window_overlap: int = 1


class ChunkRequest(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    page_id: str
    block_id: str | None = None
    granularity: ChunkGranularity
    line_ids: list[str]
    attempt: int = 0


class ChunkPlan(BaseModel):
    page_id: str
    chunks: list[ChunkRequest]
    granularity: ChunkGranularity


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


class JobManifest(BaseModel):
    """Server-side record of a correction job — status, counters, and trace data."""

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
    # Line traces (Sprint 5bis) — keyed by line_id
    line_traces: dict[str, LineTrace] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM payload models
# ---------------------------------------------------------------------------


class LLMLineInput(BaseModel):
    """One line worth of context sent to the LLM (OCR text + neighbours + hyphen hints)."""

    line_id: str
    prev_text: str | None = None
    ocr_text: str
    next_text: str | None = None
    # Hyphenation fields — absent when hyphen_role == NONE
    hyphenation_role: str | None = None
    hyphen_candidate: bool | None = None
    hyphen_join_with_next: bool | None = None
    hyphen_join_with_prev: bool | None = None
    backward_join_candidate: str | None = None
    forward_join_candidate: str | None = None


class LLMUserPayload(BaseModel):
    task: str = "correct_ocr_lines"
    granularity: ChunkGranularity
    document_id: str
    page_id: str
    block_id: str | None = None
    lines: list[LLMLineInput]


class LLMLineOutput(BaseModel):
    """One corrected line returned by the LLM — paired by ``line_id`` with its input."""

    line_id: str
    corrected_text: str


class LLMResponse(BaseModel):
    lines: list[LLMLineOutput]


# ---------------------------------------------------------------------------
# Provider / model info
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    """An LLM model description as returned by ``BaseProvider.list_models``."""

    id: str
    label: str
    supports_structured_output: bool = True
    context_window: int | None = None


# HTTP DTOs (ListModelsRequest/Response, CreateJobResponse,
# JobStatusResponse, SSEEvent) live in the consumer package — see
# `app.schemas.http` in the backend. ARCHITECTURE.md §3.2 keeps the
# server-layer payloads out of alto-core.


# ---------------------------------------------------------------------------
# Line trace (Sprint 5bis — observability)
# ---------------------------------------------------------------------------


class LineTrace(BaseModel):
    """Full text trace for a single line through the correction pipeline."""

    line_id: str
    page_id: str
    source_ocr_text: str
    model_input_text: str | None = None  # ocr_text sent to LLM
    model_corrected_text: str | None = None  # raw LLM output before any post-processing
    projected_text: str | None = (
        None  # text retained after validation/reconciliation/fallback
    )
    output_alto_text: str | None = None  # text re-extracted from the output ALTO XML

    # Diagnostic metadata
    hyphen_role: str | None = None
    rewriter_path: str | None = None  # untouched / subs_only / fast_path / slow_path
    validation_status: str | None = None  # corrected / fallback / failed
    fallback_reason: str | None = None


class JobTrace(BaseModel):
    """Collection of line traces for a complete job."""

    job_id: str
    total_lines: int = 0
    lines: list[LineTrace] = Field(default_factory=list)


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "JobStatus",
    "LineStatus",
    "ChunkGranularity",
    "Provider",
    "HyphenRole",
    "PipelineEventType",
    "Coords",
    "LineManifest",
    "BlockManifest",
    "PageManifest",
    "DocumentManifest",
    "ChunkPlannerConfig",
    "ChunkRequest",
    "ChunkPlan",
    "JobManifest",
    "LLMLineInput",
    "LLMUserPayload",
    "LLMLineOutput",
    "LLMResponse",
    "ModelInfo",
    "LineTrace",
    "JobTrace",
]
