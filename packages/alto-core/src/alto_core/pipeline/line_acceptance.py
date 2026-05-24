"""Centralized line-level acceptance / fallback policy.

This module decides, for each corrected line, whether the LLM correction
is safe to accept or should fall back to the original OCR text.

Four guards are applied in order inside ``check_line``:

1. **Source similarity** — reject corrections that are too different from
   the source OCR (measured via SequenceMatcher ratio).

2. **Neighbour proximity** — reject a correction that looks more like
   a neighbouring line's OCR than its own source (text migration).

3. **Absorption** — reject when the correction looks like source + next
   (or prev + source) concatenated (line absorbed its neighbour).

A separate post-pass ``check_adjacent_duplicates`` applies:

4. **Adjacent duplication** — reject when two adjacent corrected lines
   become (near-)identical while their sources were clearly different.

All guards are intentionally conservative: on any doubt, fall back to
the original OCR text rather than risk a glissement or duplication.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Thresholds (intentionally conservative)
# ---------------------------------------------------------------------------

# Minimum SequenceMatcher ratio between source OCR and correction.
# Below this the correction is considered too different.
MIN_SOURCE_SIMILARITY = 0.35

# If the correction is more similar to a neighbour than to the source
# by at least this margin, reject it (text migration suspected).
NEIGHBOUR_MARGIN = 0.15

# Two adjacent corrected lines are considered duplicates if their
# similarity exceeds this AND their sources were below it.
DUPLICATE_THRESHOLD = 0.85
DUPLICATE_SOURCE_MIN_DIFF = 0.70  # sources must be below this to flag

# Absorption: correction must be at least this much longer than source
# AND look like source + neighbour concatenated at this similarity.
ABSORPTION_LENGTH_RATIO = 1.2
ABSORPTION_CONCAT_SIMILARITY = 0.8


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
    if sim_source < MIN_SOURCE_SIMILARITY:
        return AcceptanceResult(
            accepted=False,
            text=source_ocr,
            reason="too_different_from_source",
        )

    # --- Guard 2: neighbour proximity ---
    if prev_ocr is not None:
        sim_prev = _similarity(prev_ocr, corrected)
        if sim_prev > sim_source + NEIGHBOUR_MARGIN:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="closer_to_previous_line",
            )

    if next_ocr is not None:
        sim_next = _similarity(next_ocr, corrected)
        if sim_next > sim_source + NEIGHBOUR_MARGIN:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="closer_to_next_line",
            )

    # --- Guard 3: absorption of adjacent line ---
    # Detects when the correction is source + neighbour concatenated.
    src_len = max(len(source_ocr), 1)

    if next_ocr and len(corrected) > src_len * ABSORPTION_LENGTH_RATIO:
        concat_fwd = source_ocr + " " + next_ocr
        if _similarity(corrected, concat_fwd) > ABSORPTION_CONCAT_SIMILARITY:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="absorbs_next_line",
            )

    if prev_ocr and len(corrected) > src_len * ABSORPTION_LENGTH_RATIO:
        concat_bwd = prev_ocr + " " + source_ocr
        if _similarity(corrected, concat_bwd) > ABSORPTION_CONCAT_SIMILARITY:
            return AcceptanceResult(
                accepted=False,
                text=source_ocr,
                reason="absorbs_previous_line",
            )

    return AcceptanceResult(accepted=True, text=corrected)


def check_adjacent_duplicates(
    lines: list[tuple[str, str, str]],
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
        if sim_corrected < DUPLICATE_THRESHOLD:
            continue

        # Sources must be clearly different (otherwise the duplication is genuine)
        sim_sources = _similarity(src_a, src_b)
        if sim_sources >= DUPLICATE_SOURCE_MIN_DIFF:
            continue

        # Flag both lines
        revert[id_a] = "adjacent_duplicate_detected"
        revert[id_b] = "adjacent_duplicate_detected"

    return revert


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "AcceptanceResult",
    "check_line",
    "check_adjacent_duplicates",
]
