"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto.hyphenation`.

New code should import from `alto_core.alto.hyphenation` directly. The shim now
re-exports only what alto-core declares in `__all__` — private
helpers (`_*`) and stdlib leaks (Path, asyncio, logging, ...)
are NOT visible here. Pull those directly from alto-core if a
test legitimately needs them.
"""

from alto_core.alto.hyphenation import (
    ReconcileMetrics,
    classify_reconcile_outcome,
    enrich_chunk_lines,
    reconcile_hyphen_pair,
    should_stay_in_same_chunk,
)

__all__ = [
    "ReconcileMetrics",
    "classify_reconcile_outcome",
    "enrich_chunk_lines",
    "reconcile_hyphen_pair",
    "should_stay_in_same_chunk",
]
