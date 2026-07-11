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

from corrigenda.core.editing import EditScript
from corrigenda.core.schemas import (
    ImageRef,
    LLMUserPayload,
    ModelInfo,
    PageManifest,
    RetryPolicy,
    Usage,
)
from corrigenda.errors import ValidationError


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


class ProviderPermanentError(Exception):
    """Raised by a ``BaseProvider`` for a failure that will NEVER heal by
    retrying: invalid credentials (401/403), unknown model (404), a
    schema the vendor definitively rejects — the client-side 4xx family
    other than 429.

    P0-1 — semantics contract: the pipeline treats this as FATAL for the
    whole run. It is never retried, never downgraded, and NEVER converted
    into an OCR fallback: a run whose provider rejected every call must
    fail loudly, not report success with silently uncorrected text.
    Propagates out of :meth:`CorrectionPipeline.run` like
    :class:`~corrigenda.errors.CorrectionAborted` does — before any
    output is written.

    Deliberately NOT a ``ValueError`` (the retry classifier routes
    ``ValueError`` to the malformed-output retry branch) and NOT a
    ``CorrectionError`` (the chunk loop treats ``CorrectionError`` as a
    recoverable per-chunk condition).
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
class EditProducer(Protocol):
    """Producer contract of the edit protocol (§5.1).

    From v2.0 the LLM ``BaseProvider`` is *an implementation* of this
    contract, not the contract itself; a deterministic rules engine (§5.3)
    and a vision/VLM producer (§5.2 bis) are others. A producer returns an
    :class:`~corrigenda.core.editing.EditScript` plus optional token
    :class:`~corrigenda.core.schemas.Usage`.

    ``wants_geometry`` / ``wants_image`` let the compiler include the
    physical anchor envelope (line geometry, opaque page image reference)
    ONLY for producers that consume it — a text producer keeps a lean
    payload. A producer with ``wants_image=True`` run without a matching
    ``source_images`` entry is a start-up error (:func:`require_source_images`),
    never a silent image-less call.
    """

    wants_geometry: bool
    wants_image: bool

    async def produce(
        self, payload: LLMUserPayload, *, policy: RetryPolicy
    ) -> tuple[EditScript, Usage | None]: ...


def require_source_images(
    producer: EditProducer,
    source_files: list[str],
    source_images: dict[str, ImageRef] | None,
) -> None:
    """Raise :class:`ValidationError` if a vision producer has no images (§5.1).

    A producer that does not want images is always fine. A vision producer
    needs a ``source_images`` mapping covering every source file — otherwise
    the run would issue an image-less VLM call, which the spec forbids.
    """
    if not getattr(producer, "wants_image", False):
        return
    if not source_images:
        raise ValidationError(
            "producer requires page images (wants_image=True) but run() "
            "received no source_images mapping"
        )
    missing = [s for s in source_files if s not in source_images]
    if missing:
        raise ValidationError(
            f"producer requires page images but source_images is missing "
            f"entries for: {sorted(missing)}"
        )


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
    "EditProducer",
    "FormatAdapter",
    "OutputWriter",
    "PipelineObserver",
    "ProviderTransientError",
    "ProviderPermanentError",
    "RewriteMetrics",
    "require_source_images",
]
