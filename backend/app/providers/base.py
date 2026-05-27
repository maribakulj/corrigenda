"""HTTP helpers for the bundled provider implementations.

The LLM contract — :class:`BaseProvider`, :data:`OUTPUT_JSON_SCHEMA`,
:data:`SYSTEM_PROMPT` — was moved to :mod:`alto_core.protocols.provider`
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

# Re-exports — public LLM contract lives in alto-core now.
from alto_core.protocols.provider import (  # noqa: F401  re-exported
    OUTPUT_JSON_SCHEMA,
    SYSTEM_PROMPT,
    BaseProvider,
    ProviderTransientError,
)

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
    """Return a ``ProviderTransientError`` chained to ``exc`` when ``exc``
    is one of the known transient httpx classes; otherwise return
    ``exc`` unchanged so the caller's raise leaves the original
    traceback intact.

    httpx.HTTPStatusError is intentionally split: 4xx (other than 429)
    is a client-side bug — bad credentials, malformed schema — that
    won't heal on retry, so we leave it alone. 5xx and 429 ARE
    transient. The split happens here rather than at the catch site so
    the pipeline doesn't need to know httpx status semantics.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 4xx is client error — only 429 (rate-limit) is worth retrying.
        if 400 <= status < 500 and status != 429:
            return exc
        return ProviderTransientError(
            str(exc), status_code=status
        ).with_traceback(exc.__traceback__)
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
