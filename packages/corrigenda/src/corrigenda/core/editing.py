"""The span edit protocol (spec §4) — types, normalisation, application.

An ``EditScript`` is the seam the spec inserts between the compiler
(``enrich_chunk_lines`` + payload) and the recomposer (the format
rewriters): a producer returns edit *operations* instead of raw corrected
text, and this module turns them into per-line corrected text the
rewriter already knows how to write back.

Two operations, no structural ones (invariant I2 is guaranteed by the
type, not by a check):

  - ``ReplaceLine`` — the whole line's text (the historical LLM response,
    re-expressed as one op). Byte-for-byte the old behaviour: applying it
    is just ``text_by_id[line_id] = op.text``.
  - ``ReplaceSpan`` — replace a sub-range of the line's *canonical* text,
    anchored either by explicit offsets (``RangeAnchor``, deterministic
    producers) or by an exact substring (``MatchAnchor``, LLM producers).
    Every ``MatchAnchor`` normalises to a ``RangeAnchor`` against the
    canonical text; an unfound / ambiguous / out-of-range anchor rejects
    the op (fallback keeps the line, invariant I2).

Invariants E1–E6 (§4.4). E1–E3 are structural, E4/E5 are span-only drift
guards; **E4/E5 never touch ``ReplaceLine``**, which the downstream
three-stage guard matrix (E6) already governs — that is what keeps the
re-expression byte-parity. E6 itself is applied later by the pipeline, at
the line level, identically for both ops.

Pure core: imports only ``core.schemas`` / ``core.pairing`` (no lxml, no
format, no producer) — the import-contract test keeps it that way.
"""

from __future__ import annotations

import hashlib
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Annotated

from corrigenda.core._norm import has_line_separator
from corrigenda.core.pairing import HYPHEN_CHARS
from corrigenda.core.schemas import (
    DEFAULT_GUARD_CONFIG,
    GuardConfig,
    HyphenRole,
    LineManifest,
)
from corrigenda.errors import ProposalValidationError

#: Version of THIS edit protocol (P3.10). Bumped only on a breaking
#: change to the op/anchor semantics; ``apply_edit_script`` refuses a
#: script stamped with a version it does not speak.
EDIT_PROTOCOL_VERSION = "1"


def line_digest(text: str) -> str:
    """Stable 16-hex digest of one line's canonical text (P3.10).

    The unit of the script's per-line preconditions: same shape as the
    §11 policy fingerprints. Consumers building scripts by hand compute
    theirs with this exact function.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Anchors (§4.3)
# ---------------------------------------------------------------------------


class RangeAnchor(BaseModel):
    """Offsets into the line's canonical text (deterministic producers)."""

    model_config = ConfigDict(frozen=True)
    start: int
    end: int


class MatchAnchor(BaseModel):
    """An exact substring of the canonical text (LLM producers).

    ``occurrence`` selects the n-th (0-indexed) occurrence. Honouring the
    §4.3 uniqueness intent (the convergent practice of aider's
    search/replace and Anthropic's ``str_replace``), the default ``None``
    *requires uniqueness*: a match found more than once is ambiguous and
    the op is rejected. An explicit integer — **including 0 for "the
    first occurrence"** — always selects that occurrence.

    ``occurrence`` is ``int | None``, never defaulted to ``0``: a
    default would conflate "producer said nothing" with "producer wants
    the first occurrence", making the first of several repeats
    inexpressible (0 + multiple matches → rejected as ambiguous).
    """

    model_config = ConfigDict(frozen=True)
    match: str
    occurrence: int | None = None


# ---------------------------------------------------------------------------
# Operations (§4.2)
# ---------------------------------------------------------------------------


class ReplaceLine(BaseModel):
    op: Literal["replace_line"] = "replace_line"
    line_id: str
    text: str
    # line_ids may legitimately repeat across
    # FILES; only page_ids are document-unique. The final edit_script stamps
    # this so a consumer can attribute every op to its file. Optional and
    # additive: hand-written scripts without it keep their old semantics,
    # and per the CorrectionReport contract a new optional key does NOT
    # bump report_version.
    page_id: str | None = None
    #: Phase 1 (ROADMAP V3) — the producer's self-assessment of THIS
    #: proposal in [0, 1], already VERIFIED app-side when it comes from
    #: the LLM uncertainty channel (claims checked against the confusion
    #: table / lexicon — the model supplies auditable evidence, never a
    #: raw score). Feeds the ``producer`` component of
    #: :class:`~corrigenda.core.schemas.LineConfidence`. Optional and
    #: additive; ``None`` = the producer declared nothing.
    producer_confidence: float | None = None


class ReplaceSpan(BaseModel):
    op: Literal["replace_span"] = "replace_span"
    line_id: str
    anchor: Union[MatchAnchor, RangeAnchor]
    text: str
    #: See ``ReplaceLine.page_id``.
    page_id: str | None = None


EditOp = Annotated[Union[ReplaceLine, ReplaceSpan], Field(discriminator="op")]


class LinePrecondition(BaseModel):
    """What one targeted line's SOURCE text must be for the script to
    apply to it (P3.10).

    ``digest`` is :func:`line_digest` of the canonical source text at
    script-build time. ``page_id`` qualifies the line like the ops'
    stamp does (bare line_ids repeat across files); ``None`` keeps
    hand-written single-file scripts simple.
    """

    model_config = ConfigDict(frozen=True)
    line_id: str
    page_id: str | None = None
    digest: str


class EditScript(BaseModel):
    ops: list[EditOp] = Field(default_factory=list)
    #: P3.10 — the protocol this script speaks. Scripts built by this
    #: library stamp the current version; ``apply_edit_script`` raises
    #: on a version it does not know. ``None`` (hand-written / legacy
    #: JSON) is accepted as the current version.
    protocol_version: str | None = None
    #: P3.10 — source file name → ``sha256:<hex>`` of the INPUT bytes
    #: the script was derived from (same shape as
    #: ``RunProvenance.source_digests``). Recorded for consumers
    #: replaying against files; not verifiable by ``apply_edit_script``
    #: itself, which sees only canonical text.
    source_digests: dict[str, str] = Field(default_factory=dict)
    #: P3.10 — per targeted line, the digest of the source text the
    #: ops were computed against. ``apply_edit_script`` REJECTS the
    #: line's ops when the document at hand carries the same line_id
    #: with different content — an op must never land on a lookalike.
    preconditions: list[LinePrecondition] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Result / rejection reporting
# ---------------------------------------------------------------------------


class EditRejection(BaseModel):
    """One op that could not be applied — the line keeps its prior text."""

    line_id: str
    op: str
    reason: str  # short machine code (see the constants below)
    detail: str = ""


# Rejection reason codes.
R_UNKNOWN_LINE = "e1_unknown_line"
R_CONFLICT = "conflict"  # >1 replace_line, or replace_line mixed with spans
R_EMPTY = "e3_empty"
R_NEWLINE = "e3_newline"
R_OVERLAP = "e2_overlap"
R_DRIFT_RATIO = "e4_span_growth"
R_DRIFT_BUDGET = "e4_line_budget"
R_HYPHEN = "e5_hyphen"
R_ANCHOR_NOT_FOUND = "anchor_not_found"
R_ANCHOR_AMBIGUOUS = "anchor_ambiguous"
R_ANCHOR_RANGE = "anchor_out_of_range"
R_ANCHOR_EMPTY = "anchor_empty_match"
R_PRECONDITION = "precondition_source_digest"


class EditResult(BaseModel):
    """Outcome of applying an ``EditScript``.

    ``text_by_id`` holds the new canonical text for every line that had at
    least one *accepted* op. Lines whose ops were all rejected are absent —
    the caller keeps their prior text (OCR fallback, invariant I2).
    """

    text_by_id: dict[str, str] = Field(default_factory=dict)
    rejected: list[EditRejection] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Anchor normalisation (§4.3)
# ---------------------------------------------------------------------------


def normalize_anchor(
    anchor: MatchAnchor | RangeAnchor, canonical: str
) -> tuple[RangeAnchor | None, str | None]:
    """Normalise any anchor to a ``RangeAnchor`` against ``canonical``.

    Returns ``(range, None)`` on success or ``(None, reason)`` on rejection.
    """
    if isinstance(anchor, RangeAnchor):
        if 0 <= anchor.start <= anchor.end <= len(canonical):
            return anchor, None
        return None, R_ANCHOR_RANGE

    if anchor.match == "":
        return None, R_ANCHOR_EMPTY

    starts: list[int] = []
    i = canonical.find(anchor.match)
    while i != -1:
        starts.append(i)
        i = canonical.find(anchor.match, i + 1)

    if not starts:
        return None, R_ANCHOR_NOT_FOUND
    if anchor.occurrence is None:
        # No explicit occurrence: the match must be unique.
        if len(starts) > 1:
            return None, R_ANCHOR_AMBIGUOUS
        s = starts[0]
    else:
        # Explicit occurrence — 0 legitimately names the first of several.
        if anchor.occurrence < 0 or anchor.occurrence >= len(starts):
            return None, R_ANCHOR_RANGE
        s = starts[anchor.occurrence]
    return RangeAnchor(start=s, end=s + len(anchor.match)), None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _has_newline(text: str) -> bool:
    # Twin of the validator's single-line gate: every
    # str.splitlines boundary counts, not just \n/\r (the shared
    # predicate keeps the two enforcement points from drifting).
    return has_line_separator(text)


def _changed_chars(original: str, replacement: str) -> int:
    """Characters actually changed by replacing ``original`` with
    ``replacement`` — the size of the differing window after trimming the
    common prefix and suffix.

    The E4 line budget must NOT sum ``abs(len(replacement) -
    len(original))``: a length-*neutral* rewrite of 100 characters cost 0,
    so ``edit_line_max_changed_chars`` bounded length drift, not the
    amount of text changed — much weaker than the invariant's name. The
    trimmed-window size is cheap, deterministic, and never underestimates
    the edit (it upper-bounds the Levenshtein distance): identical texts
    cost 0, a pure insertion/deletion costs its length, a full rewrite
    costs the larger side.
    """
    if original == replacement:
        return 0
    p = 0
    max_p = min(len(original), len(replacement))
    while p < max_p and original[p] == replacement[p]:
        p += 1
    s = 0
    max_s = min(len(original), len(replacement)) - p
    while (
        s < max_s
        and original[len(original) - 1 - s] == replacement[len(replacement) - 1 - s]
    ):
        s += 1
    return max(len(original), len(replacement)) - p - s


def _e5_hyphen_ok(role: HyphenRole, result_text: str) -> bool:
    """E5 — a hyphenated line edited by span must keep its trailing hyphen
    (forward side) and a non-empty boundary word (guaranteed by the
    non-empty result check). The full pair reconciliation runs later (E6)."""
    if role in (HyphenRole.PART1, HyphenRole.BOTH):
        return result_text.rstrip().endswith(HYPHEN_CHARS)
    return True


def _apply_spans(canonical: str, ranges: list[tuple[RangeAnchor, str]]) -> str:
    """Apply non-overlapping (range, replacement) pairs right-to-left."""
    text = canonical
    for anchor, replacement in sorted(ranges, key=lambda rt: rt[0].start, reverse=True):
        text = text[: anchor.start] + replacement + text[anchor.end :]
    return text


def _apply_line_ops(
    line_id: str,
    ops: list[ReplaceLine | ReplaceSpan],
    canonical: str,
    role: HyphenRole,
    guard: GuardConfig,
    rejected: list[EditRejection],
) -> str | None:
    """Apply one line's ops. Returns the new text, or ``None`` if the line
    should keep its prior text (every op rejected / a fatal conflict)."""
    line_ops = [o for o in ops if isinstance(o, ReplaceLine)]
    span_ops = [o for o in ops if isinstance(o, ReplaceSpan)]

    # --- replace_line: whole-line path (E1/E3/conflict only; NO E4/E5) ---
    if line_ops:
        if len(line_ops) > 1 or span_ops:
            rejected.append(
                EditRejection(line_id=line_id, op="replace_line", reason=R_CONFLICT)
            )
            return None
        text = line_ops[0].text
        if _has_newline(text):
            rejected.append(
                EditRejection(line_id=line_id, op="replace_line", reason=R_NEWLINE)
            )
            return None
        if text.strip() == "":
            rejected.append(
                EditRejection(line_id=line_id, op="replace_line", reason=R_EMPTY)
            )
            return None
        return text

    # --- replace_span: normalise, E2 overlap, E4 drift, apply, E3/E5 ---
    normalized: list[tuple[RangeAnchor, str, ReplaceSpan]] = []
    for sp in span_ops:
        if _has_newline(sp.text):
            rejected.append(
                EditRejection(line_id=line_id, op="replace_span", reason=R_NEWLINE)
            )
            continue
        rng, reason = normalize_anchor(sp.anchor, canonical)
        if rng is None:
            rejected.append(
                EditRejection(
                    line_id=line_id, op="replace_span", reason=reason or R_ANCHOR_RANGE
                )
            )
            continue
        # E4 — per-op growth ratio (span-only).
        span_len = rng.end - rng.start
        if len(sp.text) > guard.edit_span_max_growth_ratio * max(1, span_len):
            rejected.append(
                EditRejection(line_id=line_id, op="replace_span", reason=R_DRIFT_RATIO)
            )
            continue
        normalized.append((rng, sp.text, sp))

    if not normalized:
        return None

    # E2 — no overlap between accepted spans (ascending by start, then end
    # so a zero-length insertion at p sorts before a replacement at p).
    normalized.sort(key=lambda t: (t[0].start, t[0].end))
    accepted: list[tuple[RangeAnchor, str]] = []
    changed_chars = 0
    prev_start = -1
    prev_end = -1
    for rng, text, _sp in normalized:
        # A replacement whose interval crosses into the previous span
        # overlaps. A zero-length insertion at the SAME start offset as an
        # already-accepted op is equally illegal: it shares a position with
        # that op, and _apply_spans (right-to-left, stable on equal starts)
        # would apply the two in an ambiguous order — the insertion could
        # land inside the replacement's original range, leaving a character
        # the replacement was meant to remove. Co-located ops (equal start)
        # are therefore rejected regardless of length.
        if rng.start < prev_end or rng.start == prev_start:
            rejected.append(
                EditRejection(line_id=line_id, op="replace_span", reason=R_OVERLAP)
            )
            continue
        accepted.append((rng, text))
        # Count the characters the op actually changes, not the
        # length delta (see _changed_chars).
        changed_chars += _changed_chars(canonical[rng.start : rng.end], text)
        prev_start = rng.start
        prev_end = rng.end

    if not accepted:
        return None

    # E4 — per-line changed-character budget (span-only).
    if changed_chars > guard.edit_line_max_changed_chars:
        rejected.append(
            EditRejection(line_id=line_id, op="replace_span", reason=R_DRIFT_BUDGET)
        )
        return None

    result = _apply_spans(canonical, accepted)

    # E3 — the resulting line must not be empty after strip.
    if result.strip() == "":
        rejected.append(
            EditRejection(line_id=line_id, op="replace_span", reason=R_EMPTY)
        )
        return None
    # E5 — hyphenated line must keep its trailing hyphen (forward side).
    if not _e5_hyphen_ok(role, result):
        rejected.append(
            EditRejection(line_id=line_id, op="replace_span", reason=R_HYPHEN)
        )
        return None
    return result


def apply_edit_script(
    script: EditScript,
    canonical_by_id: dict[str, str],
    *,
    chunk_line_ids: set[str] | None = None,
    guard_config: GuardConfig = DEFAULT_GUARD_CONFIG,
    line_by_id: dict[str, LineManifest] | None = None,
    page_id: str | None = None,
) -> EditResult:
    """Apply an ``EditScript`` and return per-line corrected text + rejections.

    ``chunk_line_ids`` (E1) bounds which lines may be edited; an op for a
    line outside it — or with no known canonical text — is rejected.
    ``line_by_id`` supplies hyphen roles for E5; when absent, lines are
    treated as role NONE (E5 is a no-op). E6 (the three-stage guard matrix)
    is NOT run here — the pipeline applies it afterwards to the resulting
    line text, identically for ``replace_line`` and ``replace_span``.

    ``page_id`` scopes replay to one page of a
    multi-file script: ops stamped with a DIFFERENT page_id are silently
    out of scope (not rejections — they belong to another file), so a
    consumer can replay the whole final edit_script one page at a time
    even when files reuse line_ids. Ops without a stamp are always in
    scope (hand-written scripts keep their historical behaviour).

    Preconditions (P3.10): a script stamped with an unknown
    ``protocol_version`` raises :class:`~corrigenda.errors.ProposalValidationError`
    — an incompatible script must fail loudly, not half-apply. A line
    whose recorded source :func:`line_digest` differs from the document
    at hand has its ops REJECTED (``precondition_source_digest``): the
    same line_id over different content is a lookalike, never a target.
    """
    if (
        script.protocol_version is not None
        and script.protocol_version != EDIT_PROTOCOL_VERSION
    ):
        raise ProposalValidationError(
            f"edit script speaks protocol version "
            f"{script.protocol_version!r}; this library speaks "
            f"{EDIT_PROTOCOL_VERSION!r} — refusing to apply a script whose "
            "semantics may have changed."
        )

    result = EditResult()

    # Precondition index, page-scoped exactly like the ops are.
    digest_by_line: dict[str, str] = {
        pc.line_id: pc.digest
        for pc in script.preconditions
        if not (
            page_id is not None and pc.page_id is not None and pc.page_id != page_id
        )
    }

    ops_by_line: dict[str, list[ReplaceLine | ReplaceSpan]] = {}
    for op in script.ops:
        if page_id is not None and op.page_id is not None and op.page_id != page_id:
            continue
        ops_by_line.setdefault(op.line_id, []).append(op)

    for line_id, ops in ops_by_line.items():
        # E1 — line must be in the targeted chunk and have canonical text.
        if (chunk_line_ids is not None and line_id not in chunk_line_ids) or (
            line_id not in canonical_by_id
        ):
            for op in ops:
                result.rejected.append(
                    EditRejection(line_id=line_id, op=op.op, reason=R_UNKNOWN_LINE)
                )
            continue

        canonical = canonical_by_id[line_id]

        # P3.10 — the document at hand must carry the SAME source text
        # the ops were computed against; same id + different content is
        # a lookalike, never a target.
        expected = digest_by_line.get(line_id)
        if expected is not None and line_digest(canonical) != expected:
            for op in ops:
                result.rejected.append(
                    EditRejection(
                        line_id=line_id,
                        op=op.op,
                        reason=R_PRECONDITION,
                        detail=(
                            f"source digest {line_digest(canonical)} != "
                            f"recorded {expected}"
                        ),
                    )
                )
            continue
        role = (
            line_by_id[line_id].hyphen_role
            if line_by_id and line_id in line_by_id
            else HyphenRole.NONE
        )
        new_text = _apply_line_ops(
            line_id, ops, canonical, role, guard_config, result.rejected
        )
        if new_text is not None:
            result.text_by_id[line_id] = new_text

    return result


def replace_line_script(text_by_id: dict[str, str]) -> EditScript:
    """Re-express a whole-line correction map as a ``replace_line`` EditScript.

    This is the bridge that lets the historical LLM response flow through
    the protocol unchanged: ``apply_edit_script(replace_line_script(m), …)``
    reproduces ``m`` for every non-empty, newline-free entry.
    """
    return EditScript(
        ops=[ReplaceLine(line_id=lid, text=t) for lid, t in text_by_id.items()]
    )


__all__ = [
    "EDIT_PROTOCOL_VERSION",
    "RangeAnchor",
    "MatchAnchor",
    "ReplaceLine",
    "ReplaceSpan",
    "EditOp",
    "EditScript",
    "EditRejection",
    "EditResult",
    "LinePrecondition",
    "line_digest",
    "normalize_anchor",
    "apply_edit_script",
    "replace_line_script",
]
