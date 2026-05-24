"""JobStore Protocol — alto-server's concern, not alto-core's.

Defined in backend because it represents the persistence + SSE fan-out
layer of the FastAPI server. ARCHITECTURE.md §8.4 keeps this Protocol
out of alto-core to avoid coupling the pure pipeline to server-side
infrastructure. When the eventual `alto-server` package is extracted
(Phase 3), this file moves there.

The in-memory implementation lives in `app.jobs.store.JobStore`; the
Protocol mirrors its shape so the existing class satisfies it via duck
typing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from alto_core.schemas import JobManifest, Provider, SSEEvent

# SSEEvent will move out of alto-core in commit 1.B (HTTP DTOs split);
# import target will switch to app.schemas.http then.


@runtime_checkable
class JobStore(Protocol):
    """In-process or out-of-process job state + SSE subscriber registry."""

    def create_job(self, provider: Provider, model: str) -> str: ...

    def get_job(self, job_id: str) -> JobManifest | None: ...

    def update_job(self, job_id: str, **kwargs: Any) -> None: ...

    def increment_counter(self, job_id: str, field: str, delta: int = 1) -> None: ...

    def emit(self, job_id: str, event: str, data: dict[str, Any]) -> None: ...

    def stream_events(self, job_id: str) -> AsyncIterator[SSEEvent]: ...
