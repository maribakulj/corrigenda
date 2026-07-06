from __future__ import annotations

import hashlib
import json
import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


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
    # F1 — emitted when a chunk's retry budget is exhausted and its lines are
    # re-planned at the next-finer granularity (PAGE→BLOCK→WINDOW→LINE).
    CHUNK_DOWNGRADED = "chunk_downgraded"
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


class DocumentManifest(BaseModel):
    """A multi-page document: the top-level structure the pipeline consumes."""

    document_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_files: list[str]
    pages: list[PageManifest]
    total_pages: int
    total_blocks: int
    total_lines: int


# ---------------------------------------------------------------------------
# Policies (frozen, injectable — §8.2)
# ---------------------------------------------------------------------------


class FrozenPolicy(BaseModel):
    """Base for the injectable, immutable policy objects (§8.2).

    Every policy is a frozen Pydantic model whose defaults reproduce the
    library's current behaviour. ``policy_fingerprint()`` returns a stable
    short hash of the sorted JSON dump, embedded in the corrected XML's
    ``processingStep`` (§11) so an output records the exact policy it was
    produced under.
    """

    model_config = ConfigDict(frozen=True)

    def policy_fingerprint(self) -> str:
        """Stable 16-hex-char hash of this policy's sorted JSON dump."""
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ChunkPlannerConfig(FrozenPolicy):
    """Tunables for the chunk planner — character + line budgets per LLM request.

    Frozen like every §8.2 policy so a run's configuration is immutable and
    fingerprintable for provenance (§11).
    """

    max_input_chars_per_request: int = 12000
    max_lines_per_request: int = 80
    line_window_size: int = 12
    line_window_overlap: int = 1


class GuardConfig(FrozenPolicy):
    """All anti-migration / acceptance thresholds in one frozen object (F13).

    The pipeline runs three stages of text-migration guards, each with its
    own thresholds (see ``pipeline/migration_guards.py`` for the A/B/C
    matrix). Pre-F13 those numbers were scattered as module constants
    across ``line_acceptance.py``, ``migration_guards.py`` and the
    validator. They are gathered here so a consumer can tune them
    coherently — **the three stages must be tuned together**: tightening
    one stage without adjusting the others can leak a migration through
    the gap.

    Every default equals the pre-F13 constant, so ``GuardConfig()`` is
    byte-for-byte compatible with the historical behaviour.

    A future ``GuardConfig.vision()`` profile (spec §5.2 bis / v2.x) will
    relax the *source-similarity* stage for VLM producers while keeping
    the inter-line migration guards intact — not shipped until a vision
    producer benchmarks it.
    """

    # --- Stage C: line-level acceptance (line_acceptance.check_line) ---
    #: Minimum SequenceMatcher ratio between source OCR and correction.
    min_source_similarity: float = 0.35
    #: Reject if the correction resembles a neighbour more than its own
    #: source by at least this margin (text migration suspected).
    neighbour_margin: float = 0.15
    #: Two adjacent corrections are duplicates above this similarity …
    duplicate_threshold: float = 0.85
    #: … but only when their sources were below this (genuinely distinct).
    duplicate_source_min_diff: float = 0.70
    #: Absorption fires only when the correction is this much longer …
    absorption_length_ratio: float = 1.2
    #: … and matches source+neighbour concatenated above this similarity.
    absorption_concat_similarity: float = 0.8

    # --- Stage B: hyphen-pair reconciliation (migration_guards) ---
    #: PART1 corrected word count may exceed OCR by at most this many.
    part1_max_word_growth: int = 1
    #: PART1 last word may grow by at most this many characters.
    part1_last_word_char_growth: int = 3
    #: PART1 total char length may grow by ratio*len + slack.
    part1_char_growth_ratio: float = 1.4
    part1_char_growth_slack: int = 8
    #: PART2 collapsed if corrected word count < ratio * OCR word count.
    part2_collapse_ratio: float = 0.4
    #: PART2 expansion allowance: OCR word count + max(floor, ratio*OCR).
    part2_expansion_floor: int = 3
    part2_expansion_ratio: float = 0.4
    #: Boundary-word continuity: shared leading-char count required …
    boundary_prefix_len: int = 2
    #: … within this corrected/OCR first-word length ratio band.
    boundary_len_ratio_min: float = 0.5
    boundary_len_ratio_max: float = 2.0

    # --- Stage A: pre-retry pair drift (validator._check_pair_drift) ---
    #: PART1 grew by more than this many words → drift (retry).
    pair_drift_part1_word_growth: int = 2
    #: PART2 checked for collapse only when OCR had at least this many words.
    pair_drift_part2_min_words: int = 2
    #: PART2 collapsed if corrected word count < ratio * OCR word count.
    pair_drift_part2_collapse_ratio: float = 0.4


#: Module-level default reused wherever a caller passes no GuardConfig, so
#: the historical behaviour needs no allocation per call.
DEFAULT_GUARD_CONFIG = GuardConfig()


class PairingPolicy(FrozenPolicy):
    """Decides whether a PART1/BOTH line may pair with the following line (F7).

    Hyphen pairing is purely sequential: the parser links a PART1/BOTH line
    to the line immediately after it, with no geometric check. That is a
    documented *assumption*, not a proven fact — a PART1 at the bottom of a
    column and an unrelated line at the top of the next column can be
    mis-paired. The downstream migration guards (stages A/B/C) already
    catch the fallout, so the default here stays permissive; this policy is
    the seam that lets a consumer harden pairing (reject partners too far
    below, or in an unrelated block) **without forking the parser**.

    Defaults reproduce the historical behaviour exactly: no geometric
    constraint, cross-block pairing allowed — ``can_pair`` always returns
    ``True``.
    """

    #: Reject a partner whose top is more than this many ALTO units below
    #: the PART1 line's bottom (``candidate.vpos - (part1.vpos + height)``).
    #: ``None`` disables the check (default = historical behaviour).
    #: Only meaningful WITHIN a page: VPOS restarts on every page, so the
    #: check is skipped for cross-page candidates (a legitimate cross-page
    #: pair would otherwise be broken by a spurious negative/huge gap).
    max_vertical_gap: int | None = None
    #: When ``True``, only pair lines in the same TextBlock. Because a
    #: cross-page partner is by definition in a different block, this also
    #: forbids cross-page pairing — intended reading of the constraint.
    #: Default ``False`` keeps the historical cross-block pairing.
    same_block_only: bool = False

    def can_pair(self, part1: LineManifest, candidate: LineManifest) -> bool:
        """Return ``True`` if ``candidate`` may be ``part1``'s PART2 partner."""
        if self.same_block_only and part1.block_id != candidate.block_id:
            return False
        if (
            self.max_vertical_gap is not None
            and part1.page_id == candidate.page_id  # VPOS comparable intra-page only
        ):
            gap = candidate.coords.vpos - (part1.coords.vpos + part1.coords.height)
            if gap > self.max_vertical_gap:
                return False
        return True


#: Module-level default reused wherever a caller passes no PairingPolicy.
DEFAULT_PAIRING_POLICY = PairingPolicy()


class RetryPolicy(FrozenPolicy):
    """Per-chunk LLM retry strategy (F9), injectable and frozen.

    Pre-F9 the temperature ramp (0.0 → 0.3 → 0.5) and the attempt cap were
    hard-coded in the pipeline, so *any* retry introduced non-determinism.
    This policy externalises them:

      - ``max_attempts`` — attempts per chunk at a given granularity.
      - ``temperatures`` — temperature per attempt (attempt *n* uses
        ``temperatures[n-1]``, clamped to the last entry). A hyphen-
        integrity violation still pins temperature to 0.0 on the next
        attempt regardless of this ramp (handled by the pipeline).
      - ``transient_backoff_base`` / ``output_backoff_base`` — the retry
        backoff is ``attempt * base`` seconds; transient-HTTP errors use
        the first, malformed-output errors the second. Hyphen violations
        retry immediately (0 s).
      - ``per_chunk_budget`` — total attempts budget for a chunk across
        all granularity downgrades (F1). Bounds the PAGE→BLOCK→WINDOW→LINE
        descent so one malformed line can't burn unbounded calls.

    ``RetryPolicy.default()`` reproduces the historical behaviour to the
    byte; ``RetryPolicy.deterministic()`` sets every temperature to 0 for
    reproducible runs.
    """

    max_attempts: int = 3
    temperatures: tuple[float, ...] = (0.0, 0.3, 0.5)
    transient_backoff_base: float = 2.0
    output_backoff_base: float = 1.0
    per_chunk_budget: int = 6

    @classmethod
    def default(cls) -> RetryPolicy:
        """The historical behaviour (temperature ramp 0.0/0.3/0.5)."""
        return cls()

    @classmethod
    def deterministic(cls) -> RetryPolicy:
        """All temperatures 0.0 — reproducible retries (same attempt cap)."""
        return cls(temperatures=(0.0,))

    def temperature_for(self, attempt: int) -> float:
        """Temperature for a 1-based attempt index (clamped to the last)."""
        if not self.temperatures:
            return 0.0
        idx = min(max(attempt, 1) - 1, len(self.temperatures) - 1)
        return self.temperatures[idx]


#: Module-level default reused wherever a caller passes no RetryPolicy.
DEFAULT_RETRY_POLICY = RetryPolicy()


class ChunkRequest(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str
    page_id: str
    block_id: str | None = None
    granularity: ChunkGranularity
    line_ids: list[str]
    # F8 — target lines the pipeline actually corrects/accepts. Any line in
    # ``line_ids`` but NOT in ``target_line_ids`` is *context only*: it is
    # still sent to the producer so a target near it keeps full surrounding
    # context, but its output is discarded here (it is a target of an
    # adjacent chunk). ``None`` means every line is a target (PAGE / BLOCK /
    # LINE granularity, and the historical default for windows).
    target_line_ids: list[str] | None = None

    def targets(self) -> list[str]:
        """The line_ids this chunk owns (all of them when unrestricted)."""
        return self.line_ids if self.target_line_ids is None else self.target_line_ids

    attempt: int = 0


class ChunkPlan(BaseModel):
    page_id: str
    chunks: list[ChunkRequest]
    granularity: ChunkGranularity


# ``Provider``, ``JobStatus`` and ``JobManifest`` (with its ``images`` map)
# are server-side concepts and live in the consumer package — see
# ``app.schemas.job`` in the backend (spec F12). The core does not enumerate
# LLM vendors or track a job's lifecycle.


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


class Usage(BaseModel):
    """Token consumption reported by a producer call (F14, §5.1).

    Returned alongside the JSON payload by ``complete_structured`` so the
    pipeline can aggregate cost across a run and surface it on the report
    and events. A producer that cannot report tokens returns ``None``
    instead of a ``Usage``; consumers map these onto their own resource
    accounting.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


# HTTP DTOs (ListModelsRequest/Response, CreateJobResponse,
# JobStatusResponse, SSEEvent) live in the consumer package — see
# `app.schemas.http` in the backend. ARCHITECTURE.md §3.2 keeps the
# server-layer payloads out of corrigenda.


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


#: Bumped on any breaking change to the CorrectionReport JSON shape (§9).
CORRECTION_REPORT_VERSION = "1.0"


class CorrectionReport(BaseModel):
    """Public, versioned correction report (§9).

    Promotes the per-line :class:`LineTrace` from a backend-internal
    ``trace.json`` to a documented output artefact with a **stable,
    versioned JSON schema**. Each line records its full journey — source
    OCR → model input → model output → projected text → re-extracted ALTO
    text — plus the rewriter path taken and any fallback reason, so a
    consumer can render a diff/preview or measure a run without re-deriving
    anything. Returned on every run and, for a dry run
    (``run(apply=False)``), it is the whole point: the report is produced
    without writing any XML.
    """

    report_version: str = CORRECTION_REPORT_VERSION
    run_id: str
    total_lines: int = 0
    lines: list[LineTrace] = Field(default_factory=list)

    @property
    def fallback_lines(self) -> list[LineTrace]:
        """Lines that fell back to OCR (a quick health signal for consumers)."""
        return [ln for ln in self.lines if ln.validation_status == "fallback"]


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "LineStatus",
    "ChunkGranularity",
    "HyphenRole",
    "PipelineEventType",
    "Coords",
    "LineManifest",
    "BlockManifest",
    "PageManifest",
    "DocumentManifest",
    "ChunkPlannerConfig",
    "FrozenPolicy",
    "GuardConfig",
    "PairingPolicy",
    "RetryPolicy",
    "ChunkRequest",
    "ChunkPlan",
    "LLMLineInput",
    "LLMUserPayload",
    "LLMLineOutput",
    "LLMResponse",
    "ModelInfo",
    "Usage",
    "LineTrace",
    "JobTrace",
    "CorrectionReport",
]
