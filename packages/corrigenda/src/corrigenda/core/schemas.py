from __future__ import annotations

import hashlib
import json
import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

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
    """Canonical event names emitted by the correction ENGINE (P3.6).

    Only events the pipeline itself (or a host reporting the pipeline's
    metrics) can emit live here. Server-side job lifecycle
    (started/completed/failed/cancelled/queued) and SSE transport
    events (keepalive/error) are the HOST's vocabulary — the demo
    backend owns them in ``app.jobs.events.JobEventType``. The wire
    strings of both enums are part of the SSE contract with the
    frontend, enforced by ``backend/tests/test_sse_event_contract.py``
    at every CI run, and stay stable across releases.
    """

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


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class Coords(BaseModel):
    """A line/block geometry box — pixels in the source image's coordinate system.

    ALTO carries an axis-aligned bounding box natively (``HPOS``/``VPOS``/
    ``WIDTH``/``HEIGHT``). PAGE XML instead encodes a **polygon**
    (``Coords@points``); the parser stores that polygon verbatim in
    ``polygon`` and derives the enclosing bbox (the four int fields) for the
    planner, which only needs a box (P1). ``polygon`` is ``None`` for ALTO —
    it has no polygon to preserve — and the rewriter never touches geometry
    on the PAGE side, so the source polygon is a read-only provenance field.
    """

    hpos: int
    vpos: int
    width: int
    height: int
    #: PAGE ``Coords@points`` verbatim (e.g. ``"617,1046 3450,1046 …"``);
    #: ``None`` for ALTO. Preserved for provenance/parity, never rewritten.
    polygon: str | None = None


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
    #: Format the sources were parsed as ("alto" | "page"), stamped by the
    #: format builders so the engine can derive the matching adapter at
    #: write time. ``None`` on hand-built manifests: writing output then
    #: requires an explicit ``format_adapter`` on the pipeline — there is
    #: no implicit default format.
    source_format: str | None = None

    # ADR-011 — the counters are DERIVED from the pages. A stored copy
    # could contradict the content (the old validator existed to catch
    # exactly that lie); a computed one cannot. ``computed_field`` keeps
    # them in the serialized shape.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_blocks(self) -> int:
        return sum(len(p.blocks) for p in self.pages)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_lines(self) -> int:
        return sum(len(p.lines) for p in self.pages)


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

    max_input_chars_per_request: int = Field(default=12000, gt=0)
    max_lines_per_request: int = Field(default=80, gt=0)
    line_window_size: int = Field(default=12, gt=0)
    line_window_overlap: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _overlap_smaller_than_window(self) -> "ChunkPlannerConfig":
        """An overlap >= the window size can never advance."""
        if self.line_window_overlap >= self.line_window_size:
            raise ValueError(
                f"line_window_overlap={self.line_window_overlap} must be "
                f"smaller than line_window_size={self.line_window_size}"
            )
        return self


class GuardConfig(FrozenPolicy):
    """All anti-migration / acceptance thresholds in one frozen object (F13).

    The pipeline runs three stages of text-migration guards, each living
    beside the control flow that acts on it (see ``core/guards.py`` for the
    A/B/C map): Stage A in ``validator._check_pair_drift`` (pre-retry),
    Stage B in ``hyphenation`` (pair reconciliation), Stage C in
    ``guards.check_line`` (line-level acceptance). Pre-F13 the numbers were
    scattered as module constants; they are gathered here so a consumer can
    tune them coherently — **the three stages must be tuned together**:
    tightening one stage without the others can leak a migration through
    the gap.

    Intentional per-stage twins (NOT accidental duplication — do not
    "dedup" them): PART1 word-growth and PART2 collapse are checked at BOTH
    Stage A and Stage B, deliberately as separate knobs so each stage tunes
    independently. Stage A (pre-retry) is more permissive — it tolerates a
    PART1 growth of 2 before forcing a retry — while Stage B (post-retry
    reconciliation) is stricter at 1 before falling back. Collapsing the
    twins would either force the two stages to share a value (removing the
    per-stage flexibility the staged design exists for) or silently change
    guard behaviour. Each twin below cross-references its partner.

    Every default equals the pre-F13 constant, so ``GuardConfig()`` is
    byte-for-byte compatible with the historical behaviour.

    A future ``GuardConfig.vision()`` profile (spec §5.2 bis / v2.x) will
    relax the *source-similarity* stage for VLM producers while keeping
    the inter-line migration guards intact — not shipped until a vision
    producer benchmarks it.
    """

    # --- Stage C: line-level acceptance (line_acceptance.check_line) ---
    #: Minimum SequenceMatcher ratio between source OCR and correction.
    min_source_similarity: float = Field(default=0.35, ge=0.0, le=1.0)
    #: Reject if the correction resembles a neighbour more than its own
    #: source by at least this margin (text migration suspected).
    neighbour_margin: float = Field(default=0.15, ge=0.0, le=1.0)
    #: Two adjacent corrections are duplicates above this similarity …
    duplicate_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    #: … but only when their sources were below this (genuinely distinct).
    duplicate_source_min_diff: float = Field(default=0.70, ge=0.0, le=1.0)
    #: Absorption fires only when the correction is this much longer …
    absorption_length_ratio: float = Field(default=1.2, gt=0.0)
    #: … and matches source+neighbour concatenated above this similarity.
    absorption_concat_similarity: float = Field(default=0.8, ge=0.0, le=1.0)

    # --- Stage B: hyphen-pair reconciliation (hyphenation._part1/2_*) ---
    #: PART1 corrected word count may exceed OCR by at most this many.
    #: Stage-B twin of ``pair_drift_part1_word_growth`` (Stage A); stricter
    #: here (1) than at Stage A (2) on purpose — see the class docstring.
    part1_max_word_growth: int = Field(default=1, ge=0)
    #: PART1 last word may grow by at most this many characters.
    part1_last_word_char_growth: int = Field(default=3, ge=0)
    #: PART1 total char length may grow by ratio*len + slack.
    part1_char_growth_ratio: float = Field(default=1.4, gt=0.0)
    part1_char_growth_slack: int = Field(default=8, ge=0)
    #: PART2 collapsed if corrected word count < ratio * OCR word count.
    #: Stage-B twin of ``pair_drift_part2_collapse_ratio`` (Stage A); same
    #: default today, kept separate so the two stages tune independently.
    part2_collapse_ratio: float = Field(default=0.4, ge=0.0, le=1.0)
    #: PART2 expansion allowance: OCR word count + max(floor, ratio*OCR).
    part2_expansion_floor: int = Field(default=3, ge=0)
    part2_expansion_ratio: float = Field(default=0.4, ge=0.0)
    #: Boundary-word continuity: shared leading-char count required …
    boundary_prefix_len: int = Field(default=2, ge=0)
    #: … within this corrected/OCR first-word length ratio band.
    boundary_len_ratio_min: float = Field(default=0.5, gt=0.0)
    boundary_len_ratio_max: float = Field(default=2.0, gt=0.0)

    @model_validator(mode="after")
    def _boundary_band_ordered(self) -> "GuardConfig":
        """An inverted ratio band would reject every boundary word."""
        if self.boundary_len_ratio_min > self.boundary_len_ratio_max:
            raise ValueError(
                f"boundary_len_ratio_min={self.boundary_len_ratio_min} must "
                f"not exceed boundary_len_ratio_max={self.boundary_len_ratio_max}"
            )
        return self

    # --- Stage A: pre-retry pair drift (validator._check_pair_drift) ---
    #: PART1 grew by more than this many words → drift (retry). Stage-A twin
    #: of ``part1_max_word_growth`` (Stage B); more permissive (2) here.
    pair_drift_part1_word_growth: int = Field(default=2, ge=0)
    #: PART2 checked for collapse only when OCR had at least this many words.
    pair_drift_part2_min_words: int = Field(default=2, ge=0)
    #: PART2 collapsed if corrected word count < ratio * OCR word count.
    #: Stage-A twin of ``part2_collapse_ratio`` (Stage B).
    pair_drift_part2_collapse_ratio: float = Field(default=0.4, ge=0.0, le=1.0)

    # --- Edit protocol E4: per-op span drift (core/editing.py) ---
    # These bound a ``replace_span`` op ONLY. ``replace_line`` (the historical
    # whole-line path) is deliberately NOT gated here — it is governed by the
    # existing three-stage guard matrix (E6), so re-expressing today's
    # response as ``replace_line`` ops stays byte-for-byte identical.
    #: A span replacement may be at most this many times as long as the span
    #: it replaces (``len(replacement) <= ratio * max(1, span_len)``).
    edit_span_max_growth_ratio: float = Field(default=4.0, gt=0.0)
    #: Total characters a line's span ops may actually change: per
    #: op, the size of the differing window after trimming the common
    #: prefix/suffix of (original span, replacement) — so a length-neutral
    #: rewrite costs its real size, not 0. Generous by default; a rules
    #: pre-pass makes small, local edits well under it.
    edit_line_max_changed_chars: int = Field(default=200, ge=0)


#: Module-level default reused wherever a caller passes no GuardConfig, so
#: the historical behaviour needs no allocation per call.
DEFAULT_GUARD_CONFIG = GuardConfig()


class PairingPolicy(FrozenPolicy):
    """Decides whether a PART1/BOTH line may pair with the following line (F7).

    Hyphen pairing is sequential — the parser proposes the next line in
    reading order — and this policy vets the proposal. The default
    is now *geometric* for **heuristic** pairs (trailing-dash detection):

    * same block — the candidate must sit BELOW the PART1 line, within
      ``max_gap_line_heights`` of the line's own height (and no more than
      ``max_rise_line_heights`` above it, tolerance for skew/overlap).
      Rejects segmentation noise and table-cell jumps.
    * different block, same page — the candidate must look like a real
      reading continuation: either *downward with horizontal overlap*
      (next block in the same column) or *upward and horizontally
      disjoint* (top of the next column; direction-agnostic, so RTL
      layouts are treated identically). A note in the margin or a block
      far below the column is rejected.
    * different page — always accepted: cross-page linking is only ever
      proposed between the last line of page N and the first line of
      page N+1 (see ``link_cross_page_hyphens``), and VPOS restarts per
      page so geometry is not comparable.
    * **explicit** pairs (ALTO ``SUBS_TYPE``/``HYP`` markup on either
      side) bypass the geometric vetting: the OCR engine asserted the
      continuation; sequential order in the engine's own serialisation
      is stronger evidence than our geometric plausibility check. NB the
      opt-in legacy vetoes (``same_block_only``, ``max_vertical_gap``)
      still apply to explicit pairs — a consumer who set them asked for
      an absolute restriction.
    * degenerate geometry — zero-height/width boxes, or the two lines
      carrying IDENTICAL boxes (block coords copied onto every line, a
      common lazy export) — accepted: there is nothing trustworthy to
      verify, and refusing would silently disable hyphenation for every
      coordinate-less document.

    ``geometric_checks=False`` restores the historical accept-everything
    behaviour exactly.
    """

    #: Reject a partner whose top is more than this many ALTO units below
    #: the PART1 line's bottom (``candidate.vpos - (part1.vpos + height)``).
    #: ``None`` disables the check (default). Legacy absolute-units knob,
    #: kept for consumers who tuned it; the relative ``*_line_heights``
    #: knobs below are the preferred interface.
    #: Only meaningful WITHIN a page: VPOS restarts on every page, so the
    #: check is skipped for cross-page candidates (a legitimate cross-page
    #: pair would otherwise be broken by a spurious negative/huge gap).
    max_vertical_gap: int | None = Field(default=None, ge=0)
    #: When ``True``, only pair lines in the same TextBlock. Because a
    #: cross-page partner is by definition in a different block, this also
    #: forbids cross-page pairing — intended reading of the constraint.
    same_block_only: bool = False
    #: Master switch for the geometric vetting of heuristic pairs.
    #: ``False`` restores the historical purely-sequential behaviour.
    geometric_checks: bool = True
    #: Max downward gap between the PART1 line's bottom and the candidate's
    #: top, in units of the PART1 line's height. Same-block candidates and
    #: cross-block downward continuations both use it.
    max_gap_line_heights: float = Field(default=3.0, ge=0.0)
    #: Tolerance for a candidate whose top sits ABOVE the PART1 line's
    #: bottom (box overlap, skewed scans), in line heights. Beyond it, an
    #: upward candidate is only plausible as a column jump (cross-block,
    #: horizontally disjoint).
    max_rise_line_heights: float = Field(default=0.5, ge=0.0)

    @staticmethod
    def _explicit(part1: LineManifest, candidate: LineManifest) -> bool:
        """Engine-asserted continuation on either side of the pair."""
        forward_explicit = (
            part1.hyphen_forward_explicit
            if part1.hyphen_role == HyphenRole.BOTH
            else part1.hyphen_source_explicit
        )
        backward_explicit = (
            candidate.hyphen_role in (HyphenRole.PART2, HyphenRole.BOTH)
            and candidate.hyphen_source_explicit
        )
        return forward_explicit or backward_explicit

    @staticmethod
    def _degenerate(c: Coords) -> bool:
        return c.height <= 0 or c.width <= 0

    def can_pair(self, part1: LineManifest, candidate: LineManifest) -> bool:
        """Return ``True`` if ``candidate`` may be ``part1``'s PART2 partner."""
        # Page-qualify the same-block veto: block IDs are reused across
        # pages (both pages export "TextBlock1"), so a bare block_id compare
        # sees EQUAL ids for a cross-page candidate and lets it through —
        # the exact opposite of the documented "forbids cross-page pairing"
        # guarantee. A cross-page candidate is by definition a different
        # block, mirroring the page-qualified max_vertical_gap veto below.
        if self.same_block_only and (
            part1.page_id != candidate.page_id or part1.block_id != candidate.block_id
        ):
            return False
        if (
            self.max_vertical_gap is not None
            and part1.page_id == candidate.page_id  # VPOS comparable intra-page only
        ):
            gap = candidate.coords.vpos - (part1.coords.vpos + part1.coords.height)
            if gap > self.max_vertical_gap:
                return False

        # --- geometric vetting (heuristic pairs, intra-page) ---
        if not self.geometric_checks:
            return True
        if part1.page_id != candidate.page_id:
            return True  # last-of-page → first-of-next by construction
        if self._explicit(part1, candidate):
            return True
        a, b = part1.coords, candidate.coords
        if self._degenerate(a) or self._degenerate(b):
            return True  # nothing to verify
        if (a.hpos, a.vpos, a.width, a.height) == (b.hpos, b.vpos, b.width, b.height):
            # Two "consecutive" lines with IDENTICAL boxes = synthetic
            # geometry (block coords copied onto every line) — treat as
            # degenerate rather than rejecting every pair in such files.
            return True
        gap = b.vpos - (a.vpos + a.height)
        below_ok = gap <= self.max_gap_line_heights * a.height
        rise_ok = gap >= -self.max_rise_line_heights * a.height

        if part1.block_id == candidate.block_id:
            return below_ok and rise_ok

        # Cross-block: downward continuation must overlap horizontally
        # (next block, same column); an upward jump must be horizontally
        # disjoint (start of another column — either side, so RTL works)
        # AND entirely above the PART1 line: a block merely *beside* the
        # column (marginal note at the same height) is not a column start.
        h_overlap = b.hpos < a.hpos + a.width and a.hpos < b.hpos + b.width
        if rise_ok:
            return below_ok and h_overlap
        return not h_overlap and (b.vpos + b.height <= a.vpos)


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

    max_attempts: int = Field(default=3, ge=1)
    temperatures: tuple[float, ...] = (0.0, 0.3, 0.5)
    transient_backoff_base: float = Field(default=2.0, ge=0.0)
    output_backoff_base: float = Field(default=1.0, ge=0.0)
    per_chunk_budget: int = Field(default=6, ge=1)

    @model_validator(mode="after")
    def _temperatures_in_range(self) -> "RetryPolicy":
        """Every provider rejects temperatures outside [0, 2]."""
        for t in self.temperatures:
            if not (0.0 <= t <= 2.0):
                raise ValueError(f"temperature {t} outside the valid [0, 2] range")
        return self

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

    attempt: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _targets_subset_of_lines(self) -> "ChunkRequest":
        """A target outside the chunk's lines would be silently
        ignored at correction time (it has no enriched input) while still
        counting as "owned" — a line lost without a trace."""
        if self.target_line_ids is not None:
            extra = set(self.target_line_ids) - set(self.line_ids)
            if extra:
                raise ValueError(
                    f"target_line_ids not contained in line_ids: {sorted(extra)!r}"
                )
        return self


class HyphenSplit(BaseModel):
    """Record of a severed forward hyphen link (ADR-010 unit SPLIT).

    Emitted by :func:`corrigenda.core.units.split_forward_link` when the
    LINE planner cuts a chain longer than ``max_lines_per_request``, and
    carried on the :class:`ChunkPlan` so the cut is a recorded unit
    operation rather than a silent pointer side effect. Line ids are
    bare on purpose: the chain walk is page-scoped, so a split never
    crosses a page, and ``page_id`` qualifies both (ADR-009).
    """

    model_config = ConfigDict(frozen=True)

    page_id: str
    tail_line_id: str
    head_line_id: str


class ChunkPlan(BaseModel):
    page_id: str
    chunks: list[ChunkRequest]
    granularity: ChunkGranularity
    #: ADR-010 — the forward links the LINE planner severed so that no
    #: still-linked pair spans two chunks (over-cap chains). Empty at
    #: every other granularity.
    hyphen_splits: list[HyphenSplit] = Field(default_factory=list)


# ``Provider``, ``JobStatus`` and ``JobManifest`` (with its ``images`` map)
# are server-side concepts and live in the consumer package — see
# ``app.schemas.job`` in the backend (spec F12). The core does not enumerate
# LLM vendors or track a job's lifecycle.


# ---------------------------------------------------------------------------
# LLM payload models
# ---------------------------------------------------------------------------


#: Opaque page-image reference (§4.1/§5.1). The library forwards it
#: verbatim from ``run(page_images=…)`` into the payload and NEVER opens
#: it — resolving/cropping/encoding pixels is the vision producer's job
#: (invariant I4). Kept a bare ``str`` (path, URL, handle) so the core
#: carries no image machinery.
ImageRef = str


class LineGeometry(BaseModel):
    """Physical anchor for a line, copied verbatim by the compiler for
    vision producers (§4.1): the line ``coords`` (ALTO bbox or PAGE polygon)
    plus the page dimensions, enough to compute a unit-free relative bbox.
    The library only *copies* these fields; it touches no pixel."""

    coords: Coords
    page_width: int
    page_height: int


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
    # Vision envelope (§4.1) — populated by the compiler only when the
    # producer asks (``wants_geometry``); ignored by text producers.
    geometry: LineGeometry | None = None


class LLMUserPayload(BaseModel):
    task: str = "correct_ocr_lines"
    granularity: ChunkGranularity
    document_id: str
    page_id: str
    block_id: str | None = None
    lines: list[LLMLineInput]
    # Vision envelope (§4.1) — opaque page image reference, populated by the
    # compiler only when the producer asks (``wants_image``); never opened.
    image_ref: ImageRef | None = None


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
    """Working text trace for a single line through the correction run.

    This is the PYTHON-side accumulator the pipeline fills as the run
    progresses (exposed on ``CorrectionResult.traces``). The report's
    JSON artefact does not serialize it: the report builder projects
    each line's trace + terminal decision into a staged
    :class:`LineOutcome` (P3.5 — report v2). The two surfaces version
    independently (see ``docs/versioning.md``).
    """

    line_id: str
    page_id: str
    source_ocr_text: str
    model_input_text: str | None = None  # ocr_text sent to LLM
    model_corrected_text: str | None = None  # raw LLM output before any post-processing
    projected_text: str | None = (
        None  # text retained after validation/reconciliation/fallback
    )
    output_alto_text: str | None = None  # text re-extracted from the output XML
    #: P3.5 — the acceptance guard's once-computed metrics (see
    #: :class:`ProposalFeatures`); surfaces on the report's decision stage.
    proposal_features: ProposalFeatures | None = None

    # Diagnostic metadata
    hyphen_role: str | None = None
    rewriter_path: str | None = None  # untouched / subs_only / fast_path / slow_path
    validation_status: str | None = None  # corrected / fallback / failed
    fallback_reason: str | None = None


# ---------------------------------------------------------------------------
# Report v2 (§9, P3.5) — one staged LineOutcome per line:
# source → proposal → decision → projection
# ---------------------------------------------------------------------------


class ProposalStage(BaseModel):
    """What the producer was asked and what it answered — absent when the
    line never reached a producer (e.g. a rules producer's uncovered
    line)."""

    input_text: str | None = None  # enriched text sent to the producer
    output_text: str | None = None  # raw producer output, pre-guards


class DecisionReason(BaseModel):
    """Structured decision motif: a machine-stable ``code`` (the family a
    consumer aggregates on — same normalization as
    ``CorrectionResult.fallback_reasons``) plus the free-text remainder."""

    code: str
    detail: str | None = None


class ProposalFeatures(BaseModel):
    """Metrics the acceptance guard computed ONCE while deciding (P3.5) —
    recorded so no consumer re-derives them. Each field is ``None`` when
    the guard's path never computed it (e.g. neighbour similarities on a
    line whose source-similarity check already rejected it, or a line
    that never went through per-line acceptance at all)."""

    #: SequenceMatcher ratio proposal ↔ source (1.0 for an identity
    #: proposal).
    source_similarity: float | None = None
    #: SequenceMatcher ratio proposal ↔ previous line's source.
    prev_similarity: float | None = None
    #: SequenceMatcher ratio proposal ↔ next line's source.
    next_similarity: float | None = None
    #: len(proposal) / len(source) (source clamped to ≥ 1 char).
    length_ratio: float | None = None


class DecisionStage(BaseModel):
    """The line's terminal decision (always present — every line ends
    ``corrected`` or ``fallback``, enforced by the DecisionSet)."""

    status: str  # corrected / fallback
    final_text: str  # the text the artefact carries
    reason: DecisionReason | None = None  # why a fallen line fell
    #: The guard's once-computed metrics for the proposal this decision
    #: judged; ``None`` for lines that never reached per-line acceptance
    #: (chunk-level fallbacks, hyphen-unit extensions, …).
    features: ProposalFeatures | None = None


class ProjectionStage(BaseModel):
    """How the decided text landed in the rewritten artefact — absent
    when no output file was rendered (e.g. ``source_files={}``)."""

    #: Text re-extracted from the very tree the output bytes were
    #: serialized from. Renamed from the v1 ``output_alto_text`` — this
    #: library writes PAGE too, the name was wrong.
    extracted_text: str | None = None
    rewriter_path: str | None = None  # untouched / subs_only / fast_path / slow_path


class LineOutcome(BaseModel):
    """One line's whole journey through a run (report v2, §9)."""

    line_id: str
    page_id: str
    hyphen_role: str | None = None
    source_text: str
    proposal: ProposalStage | None = None
    decision: DecisionStage
    projection: ProjectionStage | None = None


#: Bumped on any breaking change to the CorrectionReport JSON shape (§9).
#: 2.0 (P3.5): flat ``LineTrace`` entries became staged ``LineOutcome``
#: objects (``source_text`` / ``proposal`` / ``decision`` /
#: ``projection``); ``output_alto_text`` renamed to
#: ``projection.extracted_text``; ``fallback_reason`` became the
#: structured ``decision.reason`` (code + detail).
CORRECTION_REPORT_VERSION = "2.0"


class CorrectionReport(BaseModel):
    """Public, versioned correction report (§9).

    Each line is a staged :class:`LineOutcome` (v2, P3.5) recording its
    full journey — source → proposal (producer in/out) → decision
    (terminal status, final text, structured reason) → projection
    (re-extracted text, rewriter path) — so a consumer can render a
    diff/preview or measure a run without re-deriving anything.
    Returned on every run; the engine never persists it (ADR-011) — it
    is ``result.report``, written as ``report.json`` by
    :meth:`CorrectionResult.write` or by the host's own transaction.
    """

    report_version: str = CORRECTION_REPORT_VERSION
    run_id: str
    total_lines: int = 0
    lines: list[LineOutcome] = Field(default_factory=list)
    #: Format-specific granularity losses aggregated over the run — e.g. the
    #: PAGE rewriter reports ``words_dropped`` / ``custom_offset_stripped`` /
    #: ``alt_textequiv_dropped`` (6.2 P4/P6) here. ``None`` when the format
    #: has nothing to report (ALTO's per-path counts already live on the
    #: line outcomes). Additive and optional, so this does NOT bump
    #: ``report_version`` — the field's contract is to bump only on a
    #: *breaking* JSON change, and a new optional key is backward-compatible.
    format_losses: dict[str, int] | None = None

    @property
    def fallback_lines(self) -> list[LineOutcome]:
        """Lines that fell back to OCR (a quick health signal for consumers)."""
        return [ln for ln in self.lines if ln.decision.status == "fallback"]


# --- public surface ---
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
    "ImageRef",
    "LineGeometry",
    "LLMLineInput",
    "LLMUserPayload",
    "LLMLineOutput",
    "LLMResponse",
    "ModelInfo",
    "Usage",
    "LineTrace",
    "LineOutcome",
    "ProposalStage",
    "ProposalFeatures",
    "DecisionStage",
    "DecisionReason",
    "ProjectionStage",
    "CorrectionReport",
]
