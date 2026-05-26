"""Protocol interfaces (ports) used by the correction pipeline.

These structural-typing contracts decouple the pure pipeline logic from
its infrastructure dependencies (HTTP providers, job persistence, SSE
fan-out, output sinks). They are the architectural seam along which
`alto-core` will eventually split from `alto-server` (see ARCHITECTURE.md).

For now they live inside `app/` and are implemented by the existing
classes via duck typing — no concrete code needs to change to satisfy
these Protocols. Conformance is checked by `test_protocols_conformance.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Optional, Protocol, runtime_checkable

from app.providers.base import BaseProvider  # re-export for a single import surface
from app.schemas import JobManifest, JobStatus, Provider, SSEEvent

__all__ = [
    "BaseProvider",
    "JobStore",
    "OutputWriter",
    "PipelineObserver",
]


@runtime_checkable
class PipelineObserver(Protocol):
    """Receives lifecycle events emitted by the correction pipeline.

    The pipeline calls `on_event` synchronously after each significant
    step (chunk started/completed, retry, fallback, warning, page lifecycle,
    document lifecycle). The observer is responsible for whatever side
    effect it wants — SSE fan-out, structured logging, metrics — without
    blocking the pipeline.

    A no-op observer is acceptable; the pipeline never inspects return values.
    """

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None: ...


@runtime_checkable
class OutputWriter(Protocol):
    """Persists corrected ALTO XML and the job trace.

    Pure I/O: the writer takes pre-computed bytes/strings and persists
    them. Computing what to write (rewriting, trace assembly) is the
    pipeline's responsibility.
    """

    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None: ...

    def write_trace(self, *, traces_payload: str) -> None: ...


@runtime_checkable
class JobStore(Protocol):
    """In-process or out-of-process job state + SSE subscriber registry.

    The current `JobStore` implementation in `app.jobs.store` is in-memory
    with sync CRUD and an async event stream; the Protocol mirrors that
    shape so the existing class satisfies it via duck typing.
    """

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
    ) -> None: ...

    def emit(self, job_id: str, event: str, data: dict[str, Any]) -> None: ...

    def stream_events(self, job_id: str) -> AsyncIterator[SSEEvent]: ...
