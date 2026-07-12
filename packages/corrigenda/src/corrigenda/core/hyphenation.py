from __future__ import annotations

from dataclasses import dataclass

from corrigenda.core._norm import ncfold
from corrigenda.core.pairing import HYPHEN_CHARS, forward_partner_id
from corrigenda.core.schemas import (
    DEFAULT_GUARD_CONFIG,
    GuardConfig,
    HyphenRole,
    LineGeometry,
    LineManifest,
    LLMLineInput,
)

_SENTINEL = object()  # distinguishes "not passed" from None


# ---------------------------------------------------------------------------
# Stage-B pair-drift guards (spec §7 stage B)
#
# These predicates detect text the LLM migrated ACROSS a hyphen pair —
# PART1 extended/absorbing, PART2 collapsed/absorbing, or PART2's boundary
# word diverging from its OCR continuation. They are consumed ONLY by
# ``reconcile_hyphen_pair`` below (their sole caller), so they live here,
# beside the reconciliation control flow they gate, rather than in
# ``guards.py`` (which owns the line-level stage-C guards). Thresholds come
# from ``GuardConfig`` (F13); the three stages tune together.
# ---------------------------------------------------------------------------


def _part1_text_migrated(
    ocr_text: str,
    corrected_text: str,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> bool:
    """PART1 appears extended or pulled from PART2.

    ``True`` when any of (thresholds from ``GuardConfig``):
      - corrected word count exceeds OCR by more than
        ``part1_max_word_growth`` (text pulled in from the next line);
      - last word grew by more than ``part1_last_word_char_growth``
        characters (word completion, e.g. ``"néces" → "nécessaires"``);
      - overall char length grew past ``ratio*len + slack``.
    """
    ocr_bare = ocr_text.rstrip("-").rstrip()
    corrected_bare = corrected_text.rstrip("-").rstrip(".")

    ocr_words = ocr_bare.split()
    corrected_words = corrected_bare.split()

    if len(corrected_words) > len(ocr_words) + config.part1_max_word_growth:
        return True

    if ocr_words and corrected_words:
        ocr_last = ocr_words[-1].rstrip("-")
        corrected_last = corrected_words[-1].rstrip("-")
        if len(corrected_last) > len(ocr_last) + config.part1_last_word_char_growth:
            return True

    if (
        len(corrected_bare)
        > len(ocr_bare) * config.part1_char_growth_ratio
        + config.part1_char_growth_slack
    ):
        return True

    return False


def _part2_text_migrated(
    ocr_text: str,
    corrected_text: str,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> bool:
    """PART2 appears collapsed or pulled from the next line.

    ``True`` when (thresholds from ``GuardConfig``):
      - corrected word count is less than ``part2_collapse_ratio`` of OCR
        (text absorbed by PART1); or
      - corrected word count exceeds OCR by more than
        ``max(part2_expansion_floor, part2_expansion_ratio * OCR)``
        (text pulled in from after PART2).
    """
    ocr_words = ocr_text.split()
    corrected_words = corrected_text.split()

    if (
        ocr_words
        and len(corrected_words) < len(ocr_words) * config.part2_collapse_ratio
    ):
        return True

    expansion = max(
        config.part2_expansion_floor,
        int(len(ocr_words) * config.part2_expansion_ratio),
    )
    if len(corrected_words) > len(ocr_words) + expansion:
        return True

    return False


def _part2_boundary_word_diverged(
    ocr_text: str,
    corrected_text: str,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> bool:
    """PART2's first word lost its OCR continuity.

    The first word of PART2 is the continuation of the hyphenated word from
    PART1. If the LLM replaced it with an unrelated word the pair is
    semantically broken even when overall lengths line up. Minor OCR
    corrections (same first 2 chars, similar length) are allowed.
    """
    ocr_words = ocr_text.split()
    cor_words = corrected_text.split()

    if not ocr_words or not cor_words:
        return False  # empty cases handled by migration/empty checks

    ocr_first = ncfold(ocr_words[0])
    cor_first = ncfold(cor_words[0])

    if ocr_first == cor_first:
        return False

    prefix_len = min(config.boundary_prefix_len, len(ocr_first), len(cor_first))
    if (
        prefix_len >= config.boundary_prefix_len
        and ocr_first[:prefix_len] == cor_first[:prefix_len]
        and config.boundary_len_ratio_min
        <= len(cor_first) / max(1, len(ocr_first))
        <= config.boundary_len_ratio_max
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class ReconcileMetrics:
    """Counts of hyphen pair reconciliation outcomes."""

    coherent: int = 0  # pair accepted as corrected
    fallback: int = 0  # both sides reverted to OCR
    neutralised: int = 0  # accepted but subs_content set to None

    @property
    def total(self) -> int:
        return self.coherent + self.fallback + self.neutralised


def enrich_chunk_lines(
    line_manifests: list[LineManifest],
    all_lines_by_id: dict[str, LineManifest],
    *,
    include_geometry: bool = False,
    page_dims: dict[str, tuple[int, int]] | None = None,
) -> list[LLMLineInput]:
    """
    Build LLMLineInput list from a chunk's LineManifests.

    For each line:
    - prev_text / next_text come from all_lines_by_id lookups.
    - Hyphenation fields are populated only when hyphen_role != NONE.

    ``include_geometry`` (§4.1 vision envelope) — when ``True`` and
    ``page_dims`` supplies ``page_id -> (width, height)``, each line's
    :class:`~corrigenda.core.schemas.LineGeometry` (its coords + page
    dimensions) is copied verbatim onto the input. Off by default so a
    text producer's payload is unchanged (byte-stable). The library only
    copies these fields; it never opens an image.
    """
    result: list[LLMLineInput] = []

    def _geometry(lm: LineManifest) -> LineGeometry | None:
        if not include_geometry or not page_dims:
            return None
        dims = page_dims.get(lm.page_id)
        if dims is None:
            return None
        return LineGeometry(coords=lm.coords, page_width=dims[0], page_height=dims[1])

    for lm in line_manifests:
        prev_text: str | None = None
        next_text: str | None = None

        if lm.prev_line_id and lm.prev_line_id in all_lines_by_id:
            prev_text = all_lines_by_id[lm.prev_line_id].ocr_text
        if lm.next_line_id and lm.next_line_id in all_lines_by_id:
            next_text = all_lines_by_id[lm.next_line_id].ocr_text

        geometry = _geometry(lm)

        if lm.hyphen_role == HyphenRole.NONE:
            result.append(
                LLMLineInput(
                    line_id=lm.line_id,
                    prev_text=prev_text,
                    ocr_text=lm.ocr_text,
                    next_text=next_text,
                    geometry=geometry,
                )
            )
        elif lm.hyphen_role == HyphenRole.BOTH:
            # Chained: PART2 of previous pair + PART1 of next pair.
            # Both join candidates exposed symmetrically.
            result.append(
                LLMLineInput(
                    line_id=lm.line_id,
                    prev_text=prev_text,
                    ocr_text=lm.ocr_text,
                    next_text=next_text,
                    hyphenation_role=lm.hyphen_role.value,
                    hyphen_candidate=True,
                    hyphen_join_with_next=True,
                    hyphen_join_with_prev=True,
                    backward_join_candidate=lm.hyphen_subs_content or None,
                    forward_join_candidate=lm.hyphen_forward_subs_content or None,
                    geometry=geometry,
                )
            )
        elif lm.hyphen_role == HyphenRole.PART1:
            result.append(
                LLMLineInput(
                    line_id=lm.line_id,
                    prev_text=prev_text,
                    ocr_text=lm.ocr_text,
                    next_text=next_text,
                    hyphenation_role=lm.hyphen_role.value,
                    hyphen_candidate=True,
                    hyphen_join_with_next=True,
                    forward_join_candidate=lm.hyphen_subs_content or None,
                    geometry=geometry,
                )
            )
        else:
            # PART2
            result.append(
                LLMLineInput(
                    line_id=lm.line_id,
                    prev_text=prev_text,
                    ocr_text=lm.ocr_text,
                    next_text=next_text,
                    hyphenation_role=lm.hyphen_role.value,
                    hyphen_candidate=True,
                    hyphen_join_with_prev=True,
                    backward_join_candidate=lm.hyphen_subs_content or None,
                    geometry=geometry,
                )
            )

    return result


# ---------------------------------------------------------------------------
# Pair-coherence helpers — implementations live in
# corrigenda.core.guards (see its docstring for the
# stage-A/B/C migration-guard matrix). They are imported above under
# their underscore names so the call sites in reconcile_hyphen_pair
# stay unchanged.


# ---------------------------------------------------------------------------
# Main reconciliation
# ---------------------------------------------------------------------------


def reconcile_hyphen_pair(
    part1: LineManifest,
    part2: LineManifest,
    corrected_part1: str,
    corrected_part2: str,
    *,
    subs_content: str | None = _SENTINEL,  # type: ignore[assignment]
    source_explicit: bool | None = None,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> tuple[str, str, str | None]:
    """
    Validate and reconcile LLM corrections for a hyphenated pair.

    Returns (final_text_part1, final_text_part2, resolved_subs_content).

    When called for a BOTH line acting as PART1 of its forward pair,
    pass subs_content and source_explicit explicitly to avoid needing
    a copy of the manifest.

    Invariants enforced:
    - The two physical lines remain distinct.
    - No text migrates from one line to the other.
    - PART1 must still end with a trailing hyphen.
    - If either side migrated, BOTH sides fall back to OCR source and
      SUBS_CONTENT is neutralised (None).
    - For explicit pairs: if subs_content is known and the join of
      boundary fragments doesn't match, BOTH sides fall back.
    - For heuristic pairs: if the boundary word diverged, BOTH sides
      fall back.
    - No incoherent pair (mixed OCR+corrected) can survive.
    """
    # Resolve parameters: explicit overrides take precedence over manifest fields
    effective_subs = (
        part1.hyphen_subs_content if subs_content is _SENTINEL else subs_content
    )
    effective_explicit = (
        part1.hyphen_source_explicit if source_explicit is None else source_explicit
    )

    _fallback = (part1.ocr_text, part2.ocr_text, None)

    # --- Migration check (PART1 extended or PART2 collapsed) ---
    if _part1_text_migrated(part1.ocr_text, corrected_part1, config):
        return _fallback
    if _part2_text_migrated(part2.ocr_text, corrected_part2, config):
        return _fallback

    # --- PART1 must still end with a trailing hyphen ---
    # Accept the full heuristic repertoire (- U+00AC U+2E17 U+00AD), not just
    # hyphen-minus: a PAGE PART1 line legitimately ends in ``¬``/``⸗`` and a
    # correction that keeps such a hyphen must not be rejected out of hand.
    # ``-`` remains in the set, so ALTO behaviour is unchanged.
    if not corrected_part1.rstrip().endswith(HYPHEN_CHARS):
        return _fallback

    # --- Empty corrected text on either side ---
    tokens1 = corrected_part1.split()
    tokens2 = corrected_part2.split()
    if not tokens1 or not tokens2:
        return _fallback

    # =================================================================
    # Explicit mode: subs_content is the authority for coherence
    # =================================================================
    if effective_explicit:
        if effective_subs:
            # Strip the FULL hyphen repertoire, matching the widened trailing
            # hyphen gate above: a PART1 ending in ``¬``/``⸗``/soft-hyphen
            # (Fraktur/old-print) otherwise kept its break char attached, so
            # the join never equalled subs_content and every non-ASCII-hyphen
            # pair was systematically reverted to OCR.
            left_bare = tokens1[-1].rstrip("".join(HYPHEN_CHARS))
            right_fragment = tokens2[0]
            joined = left_bare + right_fragment

            if ncfold(joined) == ncfold(effective_subs):
                # The boundary join can match while PART2 has absorbed
                # trailing words from the NEXT line (e.g. "saires" →
                # "saires du roi"): the join only inspects the first
                # fragment, and the floor-3 expansion allowance in
                # _part2_text_migrated is too permissive for a short PART2.
                # In explicit mode the physical line's word count must not
                # grow — otherwise a merged line survives, violating the
                # "lines never merge" invariant (the Stage-C absorption
                # guard never re-runs on a reconciled member).
                if len(tokens2) > len(part2.ocr_text.split()):
                    return _fallback
                return corrected_part1, corrected_part2, effective_subs
            else:
                return _fallback

        # No subs_content reference — use boundary word check as safety net
        if _part2_boundary_word_diverged(part2.ocr_text, corrected_part2, config):
            return _fallback
        return corrected_part1, corrected_part2, None

    # =================================================================
    # Heuristic mode: conservative, no SUBS_CONTENT reconstruction.
    # However, if a subs_content was explicitly provided (e.g. from a
    # BOTH line's forward side), preserve it rather than discarding.
    # =================================================================
    if _part2_boundary_word_diverged(part2.ocr_text, corrected_part2, config):
        return _fallback

    preserved_subs = effective_subs if subs_content is not _SENTINEL else None
    return corrected_part1, corrected_part2, preserved_subs


def classify_reconcile_outcome(
    part1_ocr: str,
    part2_ocr: str,
    corrected_part1: str,
    corrected_part2: str,
    final_part1: str,
    final_part2: str,
    subs_content: str | None,
) -> str:
    """
    Classify the outcome of reconcile_hyphen_pair.

    Returns one of: "coherent", "fallback", "neutralised".

    - coherent: correction accepted with subs_content validated
    - fallback: reconciler reverted both sides to OCR (because
      the LLM proposed something that broke an invariant)
    - neutralised: correction accepted but subs_content is None
      (heuristic mode or no subs reference)
    """
    # If reconciler reverted to OCR and LLM had proposed something different
    # on either side, it's a fallback
    proposed_change = corrected_part1 != part1_ocr or corrected_part2 != part2_ocr
    reverted = final_part1 == part1_ocr and final_part2 == part2_ocr
    if reverted and proposed_change:
        return "fallback"
    if subs_content is not None:
        return "coherent"
    return "neutralised"


def should_stay_in_same_chunk(
    line_a: LineManifest,
    line_b: LineManifest,
) -> bool:
    """
    Return True if line_a and line_b must be in the same LLM chunk
    because they form a hyphenated pair.

    Symmetric: true when either line forward-links to the other. The
    role→field mapping is resolved by the shared ``forward_partner_id``
    primitive (PART1→pair id, BOTH→forward id), so this predicate never
    re-encodes it.
    """
    return (
        forward_partner_id(line_a) == line_b.line_id
        or forward_partner_id(line_b) == line_a.line_id
    )


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "ReconcileMetrics",
    "enrich_chunk_lines",
    "reconcile_hyphen_pair",
    "classify_reconcile_outcome",
    "should_stay_in_same_chunk",
]
