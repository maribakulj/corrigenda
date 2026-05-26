"""Backend's port surface.

Three ports come from the pure ``alto_core.protocols`` package:
``BaseProvider``, ``PipelineObserver``, ``OutputWriter``. The fourth,
``JobStore``, is server-specific (in-memory state + SSE registry) and
lives in :mod:`app.protocols.job_store`. ARCHITECTURE.md §8.4 keeps
JobStore out of alto-core.

This module re-exports all four under a single ``from app.protocols``
import surface so existing call sites don't have to track where each
Protocol is defined.
"""

from alto_core.protocols import (
    BaseProvider,
    OutputWriter,
    PipelineObserver,
)

from app.protocols.job_store import JobStore

__all__ = [
    "BaseProvider",
    "JobStore",
    "OutputWriter",
    "PipelineObserver",
]
