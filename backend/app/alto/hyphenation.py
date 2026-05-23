from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.alto._norm import ncfold
from app.schemas import HyphenRole, LLMLineInput, LineManifest

_SENTINEL = object()  # distinguishes "not passed" from None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class ReconcileMetrics:
    """Counts of hyphen pair reconciliation outcomes."""
    coherent: int = 0      # pair accepted as corrected
    fallback: int = 0      # both sides reverted to OCR
    neutralised: int = 0   # accepted but subs_content set to None

    @property
    def total(self) -> int:
        return self.coherent + self.fallback + self.neutralised


def enrich_chunk_lines(
    line_manifests: list[LineManifest],
    all_lines_by_id: dict[str, LineManifest],
) -> list[LLMLineInput]:
    """
    Build LLMLineInput list from a chunk's LineManifests.

    For each line:
    - prev_text / next_text come from all_lines_by_id lookups.
    - Hyphenation fields are populated only when hyphen_role != NONE.
    """
    result: list[LLMLineInput] = []

    for lm in line_manifests:
        prev_text: Optional[str] = None
        next_text: Optional[str] = None

        if lm.prev_line_id and lm.prev_line_id in all_lines_by_id:
            prev_text = all_lines_by_id[lm.prev_line_id].ocr_text
        if lm.next_line_id and lm.next_line_id in all_lines_by_id:
            next_text = all_lines_by_id[lm.next_line_id].ocr_text

        if lm.hyphen_role == HyphenRole.NONE:
            result.append(
                LLMLineInput(
                    line_id=lm.line_id,
                    prev_text=prev_text,
                    ocr_text=lm.ocr_text,
                    next_text=next_text,
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
                )
            )

    return result


# ---------------------------------------------------------------------------
# Pair-coherence helpers
# ---------------------------------------------------------------------------

def _part1_text_migrated(ocr_text: str, corrected_text: str) -> bool:
    """
    Return True if corrected PART1 text looks like the LLM extended the
    hyphenated word or pulled text from the next line.
    """
    ocr_bare = ocr_text.rstrip("-").rstrip()
    corrected_bare = corrected_text.rstrip("-").rstrip(".")

    ocr_words = ocr_bare.split()
    corrected_words = corrected_bare.split()

    # Word count increased significantly → text was pulled from next line
    if len(corrected_words) > len(ocr_words) + 1:
        return True

    # Same or similar word count, but last word got much longer
    # (word completion, e.g. "néces" → "nécessaires")
    if ocr_words and corrected_words:
        ocr_last = ocr_words[-1].rstrip("-")
        corrected_last = corrected_words[-1].rstrip("-")
        if len(corrected_last) > len(ocr_last) + 3:
            return True

    # Overall character length grew substantially
    if len(corrected_bare) > len(ocr_bare) * 1.4 + 8:
        return True

    return False


def _part2_text_migrated(ocr_text: str, corrected_text: str) -> bool:
    """
    Return True if corrected PART2 text is drastically different from
    original, indicating cascade propagation from a shifted PART1.
    """
    ocr_words = ocr_text.split()
    corrected_words = corrected_text.split()

    # Dramatic shrinkage → content was absorbed by previous line
    if ocr_words and len(corrected_words) < len(ocr_words) * 0.4:
        return True

    # Dramatic growth → text pulled from next line
    if len(corrected_words) > len(ocr_words) + max(3, int(len(ocr_words) * 0.4)):
        return True

    return False


def _part2_boundary_word_diverged(ocr_text: str, corrected_text: str) -> bool:
    """
    Return True if the first word of corrected PART2 is completely different
    from the first word of OCR PART2.

    The first word of PART2 is the continuation of the hyphenated word from
    PART1.  If the LLM replaced it with an unrelated word (e.g. "saires" →
    "urgentes"), the hyphen pair is semantically broken.

    Minor OCR corrections (same first 2 chars, similar length) are allowed.
    """
    ocr_words = ocr_text.split()
    cor_words = corrected_text.split()

    if not ocr_words or not cor_words:
        return False  # empty cases handled by migration/empty checks

    ocr_first = ncfold(ocr_words[0])
    cor_first = ncfold(cor_words[0])

    if ocr_first == cor_first:
        return False

    # Accept minor corrections: first 2 chars match and length ratio reasonable
    prefix_len = min(2, len(ocr_first), len(cor_first))
    if (
        prefix_len >= 2
        and ocr_first[:prefix_len] == cor_first[:prefix_len]
        and 0.5 <= len(cor_first) / max(1, len(ocr_first)) <= 2.0
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Main reconciliation
# ---------------------------------------------------------------------------

def reconcile_hyphen_pair(
    part1: LineManifest,
    part2: LineManifest,
    corrected_part1: str,
    corrected_part2: str,
    *,
    subs_content: Optional[str] = _SENTINEL,
    source_explicit: Optional[bool] = None,
) -> tuple[str, str, Optional[str]]:
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
    effective_subs = part1.hyphen_subs_content if subs_content is _SENTINEL else subs_content
    effective_explicit = part1.hyphen_source_explicit if source_explicit is None else source_explicit

    _fallback = (part1.ocr_text, part2.ocr_text, None)

    # --- Migration check (PART1 extended or PART2 collapsed) ---
    if _part1_text_migrated(part1.ocr_text, corrected_part1):
        return _fallback
    if _part2_text_migrated(part2.ocr_text, corrected_part2):
        return _fallback

    # --- PART1 must still end with a trailing hyphen ---
    if not corrected_part1.rstrip().endswith("-"):
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
            left_bare = tokens1[-1].rstrip("-")
            right_fragment = tokens2[0]
            joined = left_bare + right_fragment

            if ncfold(joined) == ncfold(effective_subs):
                return corrected_part1, corrected_part2, effective_subs
            else:
                return _fallback

        # No subs_content reference — use boundary word check as safety net
        if _part2_boundary_word_diverged(part2.ocr_text, corrected_part2):
            return _fallback
        return corrected_part1, corrected_part2, None

    # =================================================================
    # Heuristic mode: conservative, no SUBS_CONTENT reconstruction.
    # However, if a subs_content was explicitly provided (e.g. from a
    # BOTH line's forward side), preserve it rather than discarding.
    # =================================================================
    if _part2_boundary_word_diverged(part2.ocr_text, corrected_part2):
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
    subs_content: Optional[str],
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
    proposed_change = (corrected_part1 != part1_ocr or corrected_part2 != part2_ocr)
    reverted = (final_part1 == part1_ocr and final_part2 == part2_ocr)
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
    """
    # PART1 → its forward partner
    if (
        line_a.hyphen_role == HyphenRole.PART1
        and line_a.hyphen_pair_line_id == line_b.line_id
    ):
        return True
    if (
        line_b.hyphen_role == HyphenRole.PART1
        and line_b.hyphen_pair_line_id == line_a.line_id
    ):
        return True
    # BOTH → its forward partner (via forward_pair_id)
    if (
        line_a.hyphen_role == HyphenRole.BOTH
        and line_a.hyphen_forward_pair_id == line_b.line_id
    ):
        return True
    if (
        line_b.hyphen_role == HyphenRole.BOTH
        and line_b.hyphen_forward_pair_id == line_a.line_id
    ):
        return True
    return False
