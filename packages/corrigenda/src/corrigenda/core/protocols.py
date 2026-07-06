"""Ports of the pure core (§3): every seam a consumer can plug into.

Structural-typing contracts decoupling the pipeline from its
infrastructure: the LLM client (``BaseProvider``), the event sink
(``PipelineObserver``), the persistence target (``OutputWriter``) and —
since the §3 reorganisation — the FORMAT seam (``FormatAdapter``),
through which the pipeline reads/writes concrete transcription XML
without importing any format module (core stays lxml-free by
construction; the import-contract test enforces it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from corrigenda.core.schemas import ModelInfo, PageManifest, Usage


class ProviderTransientError(Exception):
    """Raised by a ``BaseProvider`` to signal a recoverable transport
    failure (network timeout, 5xx upstream, connection reset, …).

    The pipeline's retry classifier uses ``isinstance(exc,
    ProviderTransientError)`` to route the error to the
    exponential-backoff branch. Providers should wrap the underlying
    library exception (``httpx.HTTPStatusError``,
    ``httpx.TimeoutException``, …) and re-raise as
    ``ProviderTransientError`` — that way corrigenda stays
    http-library-agnostic without resorting to fragile class-name
    string matching at the catch site.

    When the underlying failure was HTTP, the originating status code
    is preserved on ``status_code`` so observers can route on it (e.g.,
    distinguish 429 rate-limit from 503 upstream-blip without parsing
    the message). Transport-level failures (timeouts, network errors)
    leave ``status_code`` as ``None``. The full underlying exception is
    additionally reachable via ``__cause__`` when callers raise as
    ``raise wrapped from original``.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class BaseProvider(Protocol):
    """LLM client contract used by the pipeline.

    Implementations call out to their provider's API (or run a local
    model) and return the JSON shape declared by ``OUTPUT_JSON_SCHEMA``.
    Implementations SHOULD wrap recoverable transport failures as
    ``ProviderTransientError`` so the pipeline retries with
    exponential backoff.
    """

    async def list_models(self, api_key: str) -> list[ModelInfo]: ...

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], Usage | None]:
        """Return ``(parsed_json, usage)`` (F14).

        ``parsed_json`` matches ``OUTPUT_JSON_SCHEMA``; ``usage`` reports
        token consumption for the call, or ``None`` when the provider
        cannot report it.
        """
        ...


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


class RewriteMetrics(Protocol):
    """Structural view of a format rewriter's per-path line counts."""

    untouched: int
    subs_only: int
    fast_path: int
    slow_path: int


@runtime_checkable
class FormatAdapter(Protocol):
    """Format seam (§3): how the pipeline touches concrete XML.

    The orchestrator never imports a format module; it writes corrected
    documents and re-extracts their text exclusively through this port.
    ``corrigenda.formats.alto`` provides the ALTO implementation (and the
    pipeline's lazy composition-boundary default); ``formats.page`` will
    plug in the same way.
    """

    def rewrite_file(
        self,
        xml_path: Path,
        pages: list[PageManifest],
        provider: str,
        model: str,
        *,
        lib_version: str | None = None,
        config_fingerprint: str | None = None,
    ) -> tuple[bytes, RewriteMetrics, dict[str, str]]:
        """Rewrite one source file with the pages' corrected text.

        Returns ``(xml_bytes, metrics, line_id -> rewriter_path)``.
        """
        ...

    def extract_texts(self, xml_bytes: bytes, line_ids: set[str]) -> dict[str, str]:
        """Re-extract per-line text from rewritten XML (trace/report)."""
        ...


__all__ = [
    "BaseProvider",
    "FormatAdapter",
    "OutputWriter",
    "PipelineObserver",
    "ProviderTransientError",
    "RewriteMetrics",
]
