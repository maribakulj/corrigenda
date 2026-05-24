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

from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable

from app.providers.base import BaseProvider  # re-export for a single import surface
from app.schemas import JobManifest, JobStatus, Provider, SSEEvent

__all__ = [
    "BaseProvider",
    "PipelineObserver",
    "OutputWriter",
    "JobStore",
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

    `write_corrected` is called once per source ALTO file with the
    rewritten bytes and per-line rewriter metadata so the writer can
    update traces or compute its own observability data.

    `write_trace` is called once at the end of the pipeline with the
    full job trace (line-by-line text through every stage).
    """

    def write_corrected(
        self,
        *,
        source_name: str,
        xml_bytes: bytes,
        rewriter_paths: dict[str, str],
        output_alto_text: dict[str, str],
        file_line_ids: set[str],
    ) -> None: ...

    def write_trace(self, *, job_id: str, traces_payload: str) -> None: ...


@runtime_checkable
class JobStore(Protocol):
    """In-process or out-of-process job state + SSE subscriber registry.

    The current `JobStore` implementation in `app.jobs.store` is in-memory
    with sync CRUD and an async event stream; the Protocol mirrors that
    shape so the existing class satisfies it via duck typing.
    """

    def create_job(self, provider: Provider, model: str) -> str: ...

    def get_job(self, job_id: str) -> Optional[JobManifest]: ...

    def update_job(self, job_id: str, **kwargs: Any) -> None: ...

    def increment_counter(self, job_id: str, field: str, delta: int = 1) -> None: ...

    def emit(self, job_id: str, event: str, data: dict[str, Any]) -> None: ...

    def stream_events(self, job_id: str) -> AsyncIterator[SSEEvent]: ...
