"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.line_acceptance`.

New code should import from `alto_core.pipeline.line_acceptance` directly. This module exists
so that the existing `from app.jobs.line_acceptance import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.pipeline.line_acceptance import (  # noqa: F401  re-export
    ABSORPTION_CONCAT_SIMILARITY,
    ABSORPTION_LENGTH_RATIO,
    DUPLICATE_SOURCE_MIN_DIFF,
    DUPLICATE_THRESHOLD,
    MIN_SOURCE_SIMILARITY,
    NEIGHBOUR_MARGIN,
    AcceptanceResult,
    SequenceMatcher,
    _similarity,
    check_adjacent_duplicates,
    check_line,
    dataclass,
)
