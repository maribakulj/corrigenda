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
)

logger = logging.getLogger(__name__)


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
    rejection pattern, and status-code handling that every provider needs.
    """
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


def extract_chat_text(data: dict[str, Any], provider_label: str) -> dict[str, Any]:
    """Extract JSON content from an OpenAI-compatible chat response."""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        raise ValueError(f"{provider_label} response missing 'choices': {list(data.keys())}")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise ValueError(f"{provider_label} response has empty content in choices[0].message")
    return json.loads(content)
