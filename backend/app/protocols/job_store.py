"""JobStore Protocol — alto-server's concern, not corrigenda's.

Defined in backend because it represents the persistence + SSE fan-out
layer of the FastAPI server. ARCHITECTURE.md §8.4 keeps this Protocol
out of corrigenda to avoid coupling the pure pipeline to server-side
infrastructure. When the eventual `alto-server` package is extracted
(Phase 3), this file moves there.

The in-memory implementation lives in `app.jobs.store.JobStore`; the
Protocol mirrors its shape so the existing class satisfies it via duck
typing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from app.schemas.http import SSEEvent
from app.schemas.job import JobManifest, JobStatus, Provider


@runtime_checkable
class JobStore(Protocol):
    """In-process or out-of-process job state + SSE subscriber registry."""

    def create_job(self, provider: Provider, model: str) -> str: ...

    def get_job(self, job_id: str) -> JobManifest | None: ...

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        document_manifest: Any | None = None,
        total_lines: int | None = None,
        lines_modified: int | None = None,
        chunks_total: int | None = None,
        retries: int | None = None,
        fallbacks: int | None = None,
        duration_seconds: float | None = None,
        error: str | None = None,
        images: dict[str, str] | None = None,
        line_traces: dict[str, Any] | None = None,
        report: Any | None = None,
    ) -> None: ...

    def emit(self, job_id: str, event: str, data: dict[str, Any]) -> None: ...

    def stream_events(self, job_id: str) -> AsyncIterator[SSEEvent]: ...
