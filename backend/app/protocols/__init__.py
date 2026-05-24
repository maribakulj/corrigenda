"""Backward-compat shim. Implementation lives in :mod:`alto_core.protocols`.

New code should import from `alto_core.protocols` directly. This module exists
so that the existing `from app.protocols import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.protocols import (
    Any,
    AsyncIterator,
    BaseProvider,
    JobManifest,
    JobStore,
    OutputWriter,
    PipelineObserver,
    Protocol,
    Provider,
    SSEEvent,
    provider,
    runtime_checkable,
)
