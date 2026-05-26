from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LineStatus(str, Enum):
    PENDING = "pending"
    CORRECTED = "corrected"
    FALLBACK = "fallback"
    FAILED = "failed"


class ChunkGranularity(str, Enum):
    PAGE = "page"
    BLOCK = "block"
    WINDOW = "window"
    LINE = "line"


class Provider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    MISTRAL = "mistral"
    GOOGLE = "google"


class HyphenRole(str, Enum):
    NONE = "none"
    PART1 = "HypPart1"  # first (top) line of pair: carries left fragment + trailing hyphen
    PART2 = "HypPart2"  # second (bottom) line of pair: carries right fragment
    BOTH = "HypBoth"  # PART2 of previous pair AND PART1 of next pair (chained)


class SSEEventType(str, Enum):
    """Canonical event names emitted by the correction pipeline.

    This enum is the authoritative source of truth shared with the
    frontend's ``frontend/src/hooks/useJobStream.ts::EVENTS`` list. Any
    new event MUST appear in both places; ``tests/test_sse_event_contract``
    enforces the subset relation at every CI run.

    The string values stay stable across releases — they are part of the
    SSE wire contract.
    """

    # Pipeline lifecycle (runner)
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"

    # Document / page / chunk lifecycle (correction_pipeline)
    DOCUMENT_PARSED = "document_parsed"
    PAGE_STARTED = "page_started"
    PAGE_COMPLETED = "page_completed"
    CHUNK_PLANNED = "chunk_planned"
    CHUNK_STARTED = "chunk_started"
    CHUNK_COMPLETED = "chunk_completed"
    RETRY = "retry"
    WARNING = "warning"

    # Observability — emitted at file/job boundaries with rewriter and
    # reconcile path counts. Pure read-only diagnostics; never influence
    # the corrected XML output.
    REWRITER_STATS = "rewriter_stats"
    RECONCILE_STATS = "reconcile_stats"

    # Frontend-only initial state (kept in the enum so the contract test
    # can verify the frontend list against this set).
    QUEUED = "queued"

    # Stream keep-alive (emitted by JobStore.stream_events, not the pipeline)
    KEEPALIVE = "keepalive"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class Coords(BaseModel):
    hpos: int
    vpos: int
    width: int
    height: int


# ---------------------------------------------------------------------------
# Core line / block / page / document models
# ---------------------------------------------------------------------------


class LineManifest(BaseModel):
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
    block_id: str
    page_id: str
    block_order: int
    coords: Coords
    line_ids: list[str]


class PageManifest(BaseModel):
    page_id: str
    source_file: str
    page_index: int
    page_width: int
    page_height: int
    blocks: list[BlockManifest]
    lines: list[LineManifest]
    status: JobStatus = JobStatus.QUEUED


class DocumentManifest(BaseModel):
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
    # validate_assignment turns silent typos into ValidationError at write
    # time — paired with the typed JobStore.update_job signature, this
    # prevents a misnamed kwarg from quietly creating a bogus attribute
    # that won't round-trip on serialisation.
    model_config = {"validate_assignment": True}

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
    # Per-line text trace through every pipeline stage, keyed by
    # f"{page_id}:{line_order_global}:{line_id}" (see _trace_key).
    line_traces: dict[str, LineTrace] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM payload models
# ---------------------------------------------------------------------------


class LLMLineInput(BaseModel):
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
    line_id: str
    corrected_text: str


class LLMResponse(BaseModel):
    lines: list[LLMLineOutput]


# ---------------------------------------------------------------------------
# Provider / model info
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    id: str
    label: str
    supports_structured_output: bool = True
    context_window: int | None = None


class ListModelsRequest(BaseModel):
    provider: Provider
    api_key: str


class ListModelsResponse(BaseModel):
    provider: Provider
    models: list[ModelInfo]


# ---------------------------------------------------------------------------
# API response models
# ---------------------------------------------------------------------------


class CreateJobResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_lines: int = 0
    lines_modified: int = 0
    chunks_total: int = 0
    retries: int = 0
    fallbacks: int = 0
    duration_seconds: float | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


class SSEEvent(BaseModel):
    event: str
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Line trace — per-line observability through the correction pipeline
# ---------------------------------------------------------------------------


class LineTrace(BaseModel):
    """Full text trace for a single line through the correction pipeline."""

    line_id: str
    page_id: str
    source_ocr_text: str
    model_input_text: str | None = None  # ocr_text sent to LLM
    model_corrected_text: str | None = None  # raw LLM output before any post-processing
    projected_text: str | None = None  # text retained after validation/reconciliation/fallback
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
