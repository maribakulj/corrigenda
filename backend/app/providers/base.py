"""HTTP helpers for the bundled provider implementations.

The LLM contract — :class:`BaseProvider`, :data:`OUTPUT_JSON_SCHEMA`,
:data:`SYSTEM_PROMPT` — was moved to :mod:`corrigenda.core.protocols`
and is re-exported here so existing imports from ``app.providers.base``
keep working.

What stays in this module are the HTTP-level helpers shared by the four
provider implementations (OpenAI, Anthropic, Mistral, Google). They
will move to the future ``alto-providers`` package alongside the
concrete providers (or be replaced wholesale by XerLLM).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

# Re-exports — public LLM contract lives in corrigenda now.
from corrigenda.core.protocols import (  # noqa: F401  re-exported
    BaseProvider,
    ProviderPermanentError,
    ProviderTransientError,
)
from corrigenda.producers.llm import (  # noqa: F401  re-exported
    OUTPUT_JSON_SCHEMA,
    SYSTEM_PROMPT,
)

from app.schemas import Usage

logger = logging.getLogger(__name__)


# httpx exception classes that indicate a recoverable transport
# failure: the upstream may heal on retry. Caught here and wrapped as
# ``ProviderTransientError`` so the pipeline's retry classifier can
# route them to exponential backoff without importing httpx itself.
# 5xx and 429 from ``raise_for_status()`` fall into HTTPStatusError;
# read timeouts, connect resets, etc. into the network families.
_TRANSIENT_HTTPX_TYPES: tuple[type[BaseException], ...] = (
    httpx.HTTPStatusError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def _wrap_if_transient(exc: BaseException) -> BaseException:
    """Classify an httpx failure into the pipeline's provider taxonomy.

    httpx.HTTPStatusError is intentionally split: 4xx (other than 429)
    is a client-side rejection — bad credentials, unknown model,
    definitively refused schema — that won't heal on retry. P0-1: it is
    wrapped as ``ProviderPermanentError`` so the pipeline FAILS THE RUN
    instead of silently falling every chunk back to OCR and reporting
    success. 5xx and 429 are transient; transport-level failures too.
    The split happens here rather than at the catch site so the
    pipeline doesn't need to know httpx status semantics.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 4xx is client error — only 429 (rate-limit) is worth retrying.
        if 400 <= status < 500 and status != 429:
            return ProviderPermanentError(
                f"provider rejected the request (HTTP {status}) — check the "
                "API key, model name and request format",
                status_code=status,
            ).with_traceback(exc.__traceback__)
        return ProviderTransientError(str(exc), status_code=status).with_traceback(
            exc.__traceback__
        )
    if isinstance(exc, _TRANSIENT_HTTPX_TYPES):
        # Transport-level failures (timeout, network, protocol) carry no
        # HTTP status — status_code stays None.
        return ProviderTransientError(str(exc)).with_traceback(exc.__traceback__)
    return exc


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def call_llm(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    fallback_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Send a structured LLM request with optional 400/422 fallback.

    Centralises the httpx client lifecycle, the fallback-on-schema-
    rejection pattern, and status-code handling that every provider
    needs. Transient transport failures are re-raised as
    :class:`ProviderTransientError` so the pipeline's retry classifier
    routes them to exponential backoff.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=body,
                params=params,
                timeout=timeout,
            )

            if resp.status_code in (400, 422) and fallback_body is not None:
                logger.info("Schema rejected (%s) — retrying with fallback body", resp.status_code)
                resp = await client.post(
                    url,
                    headers=headers,
                    json=fallback_body,
                    params=params,
                    timeout=timeout,
                )

            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        wrapped = _wrap_if_transient(exc)
        if wrapped is exc:
            raise
        raise wrapped from exc


def extract_usage(data: dict[str, Any]) -> Usage | None:
    """Best-effort token usage from a provider response (F14).

    Handles the three shapes the bundled providers see:
      - OpenAI / Mistral: ``usage.{prompt_tokens, completion_tokens}``
      - Anthropic:        ``usage.{input_tokens, output_tokens}``
      - Google Gemini:    ``usageMetadata.{promptTokenCount, candidatesTokenCount}``

    Returns ``None`` when no usage block is present.
    """
    u = data.get("usage")
    if isinstance(u, dict):
        if "prompt_tokens" in u or "completion_tokens" in u:
            return Usage(
                input_tokens=int(u.get("prompt_tokens") or 0),
                output_tokens=int(u.get("completion_tokens") or 0),
            )
        if "input_tokens" in u or "output_tokens" in u:
            return Usage(
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
            )
    gm = data.get("usageMetadata")
    if isinstance(gm, dict):
        return Usage(
            input_tokens=int(gm.get("promptTokenCount") or 0),
            output_tokens=int(gm.get("candidatesTokenCount") or 0),
        )
    return None


def extract_chat_text(data: dict[str, Any], provider_label: str) -> dict[str, Any]:
    """Extract JSON content from an OpenAI-compatible chat response."""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        raise ValueError(f"{provider_label} response missing 'choices': {list(data.keys())}")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise ValueError(f"{provider_label} response has empty content in choices[0].message")
    return json.loads(content)


async def get_json(
    *,
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Send a GET and return decoded JSON, raising on HTTP errors.

    Each provider's ``list_models`` used to inline the same six-line
    ``async with httpx.AsyncClient() as client: resp = await
    client.get(...) ; resp.raise_for_status() ; return resp.json()``
    pattern. This helper centralises the client lifecycle and the
    status check so a future tweak (timeouts, retries, instrumentation)
    happens in one place.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers=headers or {},
                params=params,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        wrapped = _wrap_if_transient(exc)
        if wrapped is exc:
            raise
        raise wrapped from exc
