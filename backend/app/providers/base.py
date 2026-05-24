"""Shared protocol, system prompt, and JSON schema for all LLM providers."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from app.schemas import ModelInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------

OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "name": "ocr_correction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["lines"],
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["line_id", "corrected_text"],
                    "properties": {
                        "line_id": {"type": "string"},
                        "corrected_text": {"type": "string"},
                    },
                },
            }
        },
    },
}


# ---------------------------------------------------------------------------
# System prompt (13 rules)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Tu es un moteur de correction post-OCR spécialisé dans les documents patrimoniaux.

Règles absolues :
1. Corrige uniquement les erreurs manifestes d'OCR.
2. Conserve la langue source.
3. Conserve l'orthographe historique quand elle semble intentionnelle.
4. Ne traduis rien.
5. Ne modernise pas volontairement le texte.
6. Ne fusionne jamais deux lignes.
7. Ne scinde jamais une ligne.
8. Ne déplace jamais du texte d'une ligne à l'autre.
9. Chaque entrée line_id doit produire exactement une sortie avec le même line_id.
10. corrected_text doit contenir une seule ligne, sans caractère de saut de ligne.
11. Retourne uniquement un JSON valide conforme au schéma fourni.
12. En cas d'incertitude, fais la correction minimale.
13. Quand une ligne porte hyphenation_role="HypPart1", "HypPart2" ou "HypBoth", \
tu dois corriger chaque ligne individuellement sans déplacer de texte \
entre elles. Les mots logiques (backward_join_candidate, forward_join_candidate) \
te sont fournis à titre indicatif uniquement pour le contexte.\
"""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BaseProvider(Protocol):
    async def list_models(self, api_key: str) -> list[ModelInfo]: ...

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# HTTP helper for concrete providers
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

    This centralises the httpx client lifecycle, the fallback-on-schema-
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
