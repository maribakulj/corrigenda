"""Centralised matrix of text-migration guards across the pipeline.

The LLM occasionally tries to move text between OCR lines — completing
a hyphenated word into PART1, absorbing a neighbour into a line, or
dropping PART2 because it "looks redundant". Three stages of the
pipeline guard against this, each with its own thresholds and remedy:

  +----------+--------------------+--------------------+-------------------+
  | Stage    | Module             | Scope              | Action on hit     |
  +----------+--------------------+--------------------+-------------------+
  | A.       | pipeline/          | Hyphen pair        | Raise             |
  | Validate | validator.py       | (PART1+PART2)      | HyphenIntegrity-  |
  | (pre-    | _check_pair_drift  | word counts        | Error → retry at  |
  |  retry)  |                    |                    | temp 0.0          |
  +----------+--------------------+--------------------+-------------------+
  | B.       | pipeline/          | Hyphen pair        | Fall back to OCR  |
  | Recon-   | migration_guards   | (PART1+PART2)      | for both sides;   |
  | cile     | part1_text_*       | word counts +      | neutralise        |
  |          | part2_text_*       | char-length        | SUBS_CONTENT      |
  |          | part2_boundary_*   | + boundary word    |                   |
  +----------+--------------------+--------------------+-------------------+
  | C.       | pipeline/          | Single line vs.    | Fall back to OCR  |
  | Accept   | line_acceptance.py | source +           | for that line;    |
  | (post-   | check_line         | neighbours         | capture rejection |
  |  recon-  | check_adjacent_*   | (SequenceMatcher)  | reason            |
  |  cile)   |                    |                    |                   |
  +----------+--------------------+--------------------+-------------------+

The thresholds intentionally differ:

  - Stage A is the *strictest* — a hyphen drift is suspicious enough to
    retry the whole chunk before any fallback is applied.
  - Stage B catches drift the LLM produced *intentionally* despite the
    retry. The fallback preserves the OCR pair atomically.
  - Stage C is a *line-level* safety net that fires regardless of
    hyphen role: it catches absorption / neighbour migration that the
    pair-level guards can't see.

This module owns the pair-level helpers (stage B). Stage A and stage C
live close to their callers (validator's HyphenIntegrityError raise,
line_acceptance's per-line check_line decision) because the remedy is
tightly coupled to the local control flow. Centralising them here
would require moving HyphenIntegrityError out of validator.py and
creating a circular import — not worth the cost.

When tuning thresholds, look at all three stages: tightening one stage
without adjusting the others can leak migrations through the gap.

Stage-C helpers (``check_line``, ``check_adjacent_duplicates``) were
merged into this module by the §3 reorganisation: the three stages now
live in ONE file, matching the spec tree (core/guards.py) and making
the tune-them-together doctrine physically obvious. Stage A's raise
site stays in core/validator.py (HyphenIntegrityError's home).
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from corrigenda.core._norm import ncfold
from corrigenda.core.schemas import DEFAULT_GUARD_CONFIG, GuardConfig


def part1_text_migrated(
    ocr_text: str,
    corrected_text: str,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> bool:
    """Stage-B pair guard: PART1 appears extended or pulled from PART2.

    Returns ``True`` when any of (thresholds from ``GuardConfig``):
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


def part2_text_migrated(
    ocr_text: str,
    corrected_text: str,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> bool:
    """Stage-B pair guard: PART2 appears collapsed or pulled from next.

    Returns ``True`` when (thresholds from ``GuardConfig``):
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


def part2_boundary_word_diverged(
    ocr_text: str,
    corrected_text: str,
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> bool:
    """Stage-B pair guard: PART2's first word lost its OCR continuity.

    The first word of PART2 is the continuation of the hyphenated word
    from PART1. If the LLM replaced it with an unrelated word the
    hyphen pair is semantically broken even when overall lengths line
    up.

    Minor OCR corrections (same first 2 chars, similar length) are
    allowed.
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


# Thresholds live on ``GuardConfig`` (F13). The stage-C guards below read
# them from the passed ``config`` (defaulting to ``DEFAULT_GUARD_CONFIG``,
# whose values reproduce the historical constants byte-for-byte). See the
# migration-guard matrix in ``migration_guards.py`` — the three stages
# tune together.


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

        # Skip if either is already flagged
        if id_a in revert or id_b in revert:
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
    "part1_text_migrated",
    "part2_text_migrated",
    "part2_boundary_word_diverged",
]
