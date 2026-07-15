"""Stage-C line-level text-migration guards (spec §7 stage C).

The LLM occasionally tries to move text between OCR lines — completing a
hyphenated word into PART1, absorbing a neighbour into a line, or dropping
PART2 because it "looks redundant". The pipeline guards against this in
THREE stages, each living beside the control flow that acts on it — there
is no single "guards" module, and this docstring is the map of where each
stage lives rather than a claim to own them all:

  +----------+----------------------+------------------+-------------------+
  | Stage    | Home                 | Scope            | Action on hit     |
  +----------+----------------------+------------------+-------------------+
  | A.       | validator.py         | Hyphen pair      | Raise             |
  | Validate | _check_pair_drift    | (PART1+PART2)    | HyphenIntegrity-  |
  | (pre-    |                      | word counts      | Error → retry at  |
  |  retry)  |                      |                  | temp 0.0          |
  +----------+----------------------+------------------+-------------------+
  | B.       | hyphenation.py       | Hyphen pair      | Fall back to OCR  |
  | Recon-   | _part1_text_migrated | word counts +    | for both sides;   |
  | cile     | _part2_text_migrated | char-length +    | neutralise        |
  |          | _part2_boundary_*    | boundary word    | SUBS_CONTENT      |
  +----------+----------------------+------------------+-------------------+
  | C.       | guards.py (HERE)     | Single line vs.  | Fall back to OCR  |
  | Accept   | check_line           | source +         | for that line;    |
  | (post-   | check_adjacent_*     | neighbours       | capture rejection |
  |  recon-  |                      | (SequenceMatcher)| reason            |
  +----------+----------------------+------------------+-------------------+

The thresholds intentionally differ and tune TOGETHER (all read from
``GuardConfig``, F13): tightening one stage without the others can leak
migrations through the gap.

  - Stage A carries the *most aggressive remedy* — a hyphen drift is
    suspicious enough to retry the whole chunk before any fallback. Its
    numeric thresholds are deliberately MORE permissive than Stage B's
    (PART1 growth: 2 words at A vs 1 at B — see ``GuardConfig``): a cheap
    retry only fires on gross drift, then the strict Stage B bound decides
    what actually survives reconciliation ("strict" describes the
    remedy, not the thresholds).
  - Stage B catches drift the LLM produced despite the retry; the fallback
    preserves the OCR pair atomically. Its predicates live in
    ``hyphenation.py`` beside their sole caller (``reconcile_hyphen_pair``).
  - Stage C is a *line-level* safety net (this module) that fires
    regardless of hyphen role: it catches absorption / neighbour migration
    the pair-level guards can't see.

Each stage lives with its remedy on purpose: Stage A's raise belongs to
``HyphenIntegrityError``'s home (validator.py), Stage B's predicates to the
reconciliation flow (hyphenation.py), Stage C's decision here. Forcing them
into one file would only re-introduce the cross-module imports Stage B had
until it was moved to its caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from corrigenda.core.schemas import DEFAULT_GUARD_CONFIG, GuardConfig


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceResult:
    """Result of the acceptance check for a single line."""

    accepted: bool
    text: str  # retained text (correction or OCR fallback)
    reason: str | None = None  # None when accepted; short tag when rejected


# ---------------------------------------------------------------------------
# Similarity helper
# ---------------------------------------------------------------------------


def _similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio between two strings (0.0–1.0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_line(
    source_ocr: str,
    corrected: str,
    prev_ocr: str | None = None,
    next_ocr: str | None = None,
    *,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> AcceptanceResult:
    """Decide whether *corrected* is safe to accept for *source_ocr*.

    Parameters
    ----------
    source_ocr : str
        Original OCR text for this line.
    corrected : str
        LLM-proposed correction.
    prev_ocr : str | None
        OCR text of the previous line (if available).
    next_ocr : str | None
        OCR text of the next line (if available).

    Returns
    -------
    AcceptanceResult
        .accepted = True and .text = corrected  when safe;
        .accepted = False and .text = source_ocr when rejected.
    """
    # Identity: no change, always accept
    if corrected == source_ocr:
        return AcceptanceResult(accepted=True, text=corrected)

    # --- Guard 1: source similarity ---
    sim_source = _similarity(source_ocr, corrected)
    if sim_source < config.min_source_similarity:
        return AcceptanceResult(
            accepted=False,
            text=source_ocr,
            reason="too_different_from_source",
        )

    # --- Guard 2: neighbour proximity ---
    if prev_ocr is not None:
        sim_prev = _similarity(prev_ocr, corrected)
        if sim_prev > sim_source + config.neighbour_margin:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="closer_to_previous_line",
            )

    if next_ocr is not None:
        sim_next = _similarity(next_ocr, corrected)
        if sim_next > sim_source + config.neighbour_margin:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="closer_to_next_line",
            )

    # --- Guard 3: absorption of adjacent line ---
    # Detects when the correction is source + neighbour concatenated.
    src_len = max(len(source_ocr), 1)

    if next_ocr and len(corrected) > src_len * config.absorption_length_ratio:
        concat_fwd = source_ocr + " " + next_ocr
        if _similarity(corrected, concat_fwd) > config.absorption_concat_similarity:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="absorbs_next_line",
            )

    if prev_ocr and len(corrected) > src_len * config.absorption_length_ratio:
        concat_bwd = prev_ocr + " " + source_ocr
        if _similarity(corrected, concat_bwd) > config.absorption_concat_similarity:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="absorbs_previous_line",
            )

    return AcceptanceResult(accepted=True, text=corrected)


def check_adjacent_duplicates(
    lines: list[tuple[str, str, str]],
    *,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> dict[str, str]:
    """Detect adjacent duplicate corrections.

    Parameters
    ----------
    lines : list of (line_id, source_ocr, corrected_text)
        Ordered list of lines in the chunk, already individually accepted.

    Returns
    -------
    dict mapping line_id → fallback_reason for lines that should revert.
    Both lines of a duplicate pair are reverted.
    """
    revert: dict[str, str] = {}
    for i in range(len(lines) - 1):
        id_a, src_a, cor_a = lines[i]
        id_b, src_b, cor_b = lines[i + 1]

        # Skip only if the RIGHT line is already flagged (nothing new to
        # decide). When only the left line is already flagged we must still
        # evaluate the right one against it — otherwise a run of three or
        # more identical corrections leaves its third line unreverted
        # (i=0 flags lines 0,1; i=1 would `continue` on the flagged line 1
        # and never test line 2).
        if id_b in revert:
            continue

        # Corrections must be very similar
        sim_corrected = _similarity(cor_a, cor_b)
        if sim_corrected < config.duplicate_threshold:
            continue

        # Sources must be clearly different (otherwise the duplication is genuine)
        sim_sources = _similarity(src_a, src_b)
        if sim_sources >= config.duplicate_source_min_diff:
            continue

        # Flag both lines
        revert[id_a] = "adjacent_duplicate_detected"
        revert[id_b] = "adjacent_duplicate_detected"

    return revert


# --- __all__ ---
__all__ = [
    "AcceptanceResult",
    "check_line",
    "check_adjacent_duplicates",
]
