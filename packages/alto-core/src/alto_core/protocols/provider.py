"""LLM provider port + the JSON contract the pipeline expects.

This module is the seam between the pure pipeline and any LLM client
implementation (HTTP-based, local model, mock, etc.). Concrete providers
live outside ``alto-core``; consumers either ship their own
``BaseProvider`` implementation or use a published adapter package.

The ``SYSTEM_PROMPT`` and ``OUTPUT_JSON_SCHEMA`` constants are part of
the contract — they define what the pipeline guarantees to send and
what shape it expects back, regardless of which model is on the other
end of the wire.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from alto_core.schemas import ModelInfo

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
# Exceptions
# ---------------------------------------------------------------------------


class ProviderTransientError(Exception):
    """Raised by a ``BaseProvider`` to signal a recoverable transport
    failure (network timeout, 5xx upstream, connection reset, …).

    The pipeline's retry classifier uses ``isinstance(exc,
    ProviderTransientError)`` to route the error to the
    exponential-backoff branch. Providers should wrap the underlying
    library exception (``httpx.HTTPStatusError``,
    ``httpx.TimeoutException``, …) and re-raise as
    ``ProviderTransientError`` — that way alto-core stays
    http-library-agnostic without resorting to fragile class-name
    string matching at the catch site.
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


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
    ) -> dict[str, Any]: ...


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "BaseProvider",
    "OUTPUT_JSON_SCHEMA",
    "ProviderTransientError",
    "SYSTEM_PROMPT",
]
