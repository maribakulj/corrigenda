"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto.hyphenation`.

New code should import from `alto_core.alto.hyphenation` directly. This module exists
so that the existing `from app.alto.hyphenation import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.alto.hyphenation import (  # noqa: F401  re-export
    _SENTINEL,
    HyphenRole,
    LineManifest,
    LLMLineInput,
    ReconcileMetrics,
    _part1_text_migrated,
    _part2_boundary_word_diverged,
    _part2_text_migrated,
    classify_reconcile_outcome,
    dataclass,
    enrich_chunk_lines,
    ncfold,
    reconcile_hyphen_pair,
    should_stay_in_same_chunk,
)
