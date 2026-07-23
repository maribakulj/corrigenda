"""LLM producer contract surface: the system prompt and JSON output schema.

Moved here from ``protocols/provider`` by the §3 reorganisation: prompts
and output schemas are PRODUCER concerns, not core ports. They define
what the pipeline guarantees to send to an LLM producer and the shape it
expects back, regardless of which model is on the other end of the wire.
"""

from __future__ import annotations

from typing import Any

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
# Uncertainty channel (ROADMAP V3 Phase 1) — opt-in contract variant
# ---------------------------------------------------------------------------

#: Reason codes the model may attach to a modified token. ASCII slugs on
#: purpose (enum robustness across providers). The app VERIFIES the
#: verifiable ones — a claim is evidence to audit, never a score to
#: trust: ``confusion_connue`` is checked against the confusion table,
#: ``mot_du_lexique`` against the lexicon; ``infere_du_contexte`` is
#: honest-but-unverifiable; ``conjecture`` is the model admitting a
#: guess (which beats guessing silently).
UNCERTAINTY_REASONS: tuple[str, ...] = (
    "confusion_connue",
    "mot_du_lexique",
    "infere_du_contexte",
    "conjecture",
)

UNCERTAINTY_PROMPT_SUFFIX = """
14. Pour chaque ligne, renseigne status: "certain" si tu es sûr de ta \
correction (ou si la ligne est inchangée), "uncertain" sinon. Signaler un \
doute est toujours préférable à une correction silencieusement hasardeuse.
15. Pour chaque mot modifié, ajoute une entrée dans edits: {"source": mot \
d'origine, "corrected": mot corrigé, "reason": un code parmi \
"confusion_connue" (confusion OCR classique, ex. rn→m, ſ→s), \
"mot_du_lexique" (le mot corrigé est un mot attesté), \
"infere_du_contexte" (déduit des lignes voisines), "conjecture" (tu n'es \
pas sûr). Une ligne inchangée a edits: [].
16. Ne déclare jamais une raison que tu ne peux pas justifier : les codes \
sont vérifiés."""


def uncertainty_system_prompt() -> str:
    """The base prompt plus the uncertainty-channel rules (14-16)."""
    return SYSTEM_PROMPT + UNCERTAINTY_PROMPT_SUFFIX


def uncertainty_output_schema() -> dict[str, Any]:
    """The base schema extended with ``status`` and per-token ``edits``.

    Every property stays REQUIRED (an empty ``edits`` list is the "no
    modified token" answer) — strict structured-output modes reject
    optional keys, and a forced ``status`` choice beats an omittable
    one.
    """
    return {
        "name": "ocr_correction_with_uncertainty",
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
                        "required": ["line_id", "corrected_text", "status", "edits"],
                        "properties": {
                            "line_id": {"type": "string"},
                            "corrected_text": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["certain", "uncertain"],
                            },
                            "edits": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["source", "corrected", "reason"],
                                    "properties": {
                                        "source": {"type": "string"},
                                        "corrected": {"type": "string"},
                                        "reason": {
                                            "type": "string",
                                            "enum": list(UNCERTAINTY_REASONS),
                                        },
                                    },
                                },
                            },
                        },
                    },
                }
            },
        },
    }


__all__ = [
    "OUTPUT_JSON_SCHEMA",
    "SYSTEM_PROMPT",
    "UNCERTAINTY_PROMPT_SUFFIX",
    "UNCERTAINTY_REASONS",
    "uncertainty_output_schema",
    "uncertainty_system_prompt",
]
