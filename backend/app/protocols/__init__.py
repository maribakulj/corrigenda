"""Backend's port surface.

Two ports come from the pure ``corrigenda.core.protocols`` package:
``BaseProvider`` and ``PipelineObserver``. The other two are
server-specific: ``JobStore`` (in-memory state + SSE registry,
:mod:`app.protocols.job_store`; ARCHITECTURE.md §8.4 keeps it out of
corrigenda) and ``OutputWriter`` — since ADR-011 slice D-fin the engine
never persists, so the persistence port belongs to the BACKEND: the
JobRunner stages ``result.corrected_files`` + the §9 report through it
and owns the commit/discard transaction.

This module re-exports all four under a single ``from app.protocols``
import surface so existing call sites don't have to track where each
Protocol is defined.
"""

from typing import Protocol, runtime_checkable

from corrigenda.core.protocols import (
    BaseProvider,
    PipelineObserver,
)

from app.protocols.job_store import JobStore


@runtime_checkable
class OutputWriter(Protocol):
    """Persists corrected XML and the job trace (backend-owned port).

    Pure I/O: the writer takes pre-computed bytes/strings and persists
    them. Computing what to write is the engine's job — the JobRunner
    reads it off the ``CorrectionResult`` (ADR-011) and feeds it here.
    """

    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None: ...

    def write_trace(self, *, traces_payload: str) -> None: ...


__all__ = [
    "BaseProvider",
    "JobStore",
    "OutputWriter",
    "PipelineObserver",
]
