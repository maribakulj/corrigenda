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
"""

from __future__ import annotations

from corrigenda.alto._norm import ncfold
from corrigenda.schemas import DEFAULT_GUARD_CONFIG, GuardConfig


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
