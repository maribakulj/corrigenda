"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.chunk_planner`.

New code should import from `alto_core.pipeline.chunk_planner` directly. This module exists
so that the existing `from app.jobs.chunk_planner import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.pipeline.chunk_planner import (  # noqa: F401  re-export
    _CHAIN,
    ChunkGranularity,
    ChunkPlan,
    ChunkPlannerConfig,
    ChunkRequest,
    HyphenRole,
    LineManifest,
    PageManifest,
    _make_chunk,
    _plan_line,
    _total_chars,
    _try_block,
    _try_page,
    _try_window,
    downgrade_granularity,
    plan_page,
    should_stay_in_same_chunk,
    uuid,
)
