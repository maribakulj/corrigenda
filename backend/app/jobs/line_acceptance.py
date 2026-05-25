"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.line_acceptance`.

New code should import from `alto_core.pipeline.line_acceptance` directly. The shim now
re-exports only what alto-core declares in `__all__` — private
helpers (`_*`) and stdlib leaks (Path, asyncio, logging, ...)
are NOT visible here. Pull those directly from alto-core if a
test legitimately needs them.
"""

from alto_core.pipeline.line_acceptance import (
    AcceptanceResult,
    check_adjacent_duplicates,
    check_line,
)

__all__ = ["AcceptanceResult", "check_adjacent_duplicates", "check_line"]
