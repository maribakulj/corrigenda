"""Protocol interfaces (ports) used by the correction pipeline.

These structural-typing contracts decouple the pure pipeline logic from
its infrastructure dependencies (LLM HTTP providers, output sinks,
observer fan-out). Server-side concepts (job persistence, SSE registry)
live in the consumer package — see ``app.protocols.job_store`` in the
backend, or future ``alto_server.protocols``.

The contracts are intentionally minimal: a consumer that implements
these three Protocols can drive ``CorrectionPipeline`` against any LLM
provider, any event sink, and any persistence target.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from corrigenda.protocols.provider import (  # re-export
    BaseProvider,
    ProviderTransientError,
)

__all__ = [
    "BaseProvider",
    "OutputWriter",
    "PipelineObserver",
    "ProviderTransientError",
]


@runtime_checkable
class PipelineObserver(Protocol):
    """Receives lifecycle events emitted by the correction pipeline.

    The pipeline calls ``on_event`` synchronously after each significant
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
