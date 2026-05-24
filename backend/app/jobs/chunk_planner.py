"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.chunk_planner`.

New code should import from `alto_core.pipeline.chunk_planner` directly. The shim now
re-exports only what alto-core declares in `__all__` — private
helpers (`_*`) and stdlib leaks (Path, asyncio, logging, ...)
are NOT visible here. Pull those directly from alto-core if a
test legitimately needs them.
"""

from alto_core.pipeline.chunk_planner import (  # noqa: F401  re-export
    downgrade_granularity,
    plan_page,
)
