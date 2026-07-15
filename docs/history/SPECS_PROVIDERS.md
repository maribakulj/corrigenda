# SPECS_PROVIDERS — Fournisseurs LLM

Fichiers cibles : `backend/app/providers/`

---

## Protocole commun (`base.py`)

```python
class BaseProvider(Protocol):
    async def list_models(self, api_key: str) -> list[ModelInfo]: ...
    async def complete_structured(
        self, api_key, model, system_prompt, user_payload, json_schema, temperature=0.0
    ) -> dict: ...
```

---

## Prompt système (`base.py`)

```
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
13. Quand une ligne porte hyphenation_role="HypPart1" ou "HypPart2",
    tu dois corriger chaque ligne individuellement sans déplacer de texte
    entre elles. Le mot logique (logical_join_candidate) t'est fourni
    à titre indicatif uniquement pour le contexte.
```

---

## Schéma JSON de sortie

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["lines"],
  "properties": {
    "lines": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["line_id", "corrected_text"],
        "properties": {
          "line_id": {"type": "string"},
          "corrected_text": {"type": "string"}
        }
      }
    }
  }
}
```

---

## Exemple de payload user avec césure

```json
{
  "task": "correct_ocr_lines",
  "granularity": "window",
  "document_id": "DOC_001",
  "page_id": "P_001",
  "lines": [
    {
      "line_id": "TL_101",
      "prev_text": "Il marchait vite.",
      "ocr_text": "Il s'approcha de la por-",
      "next_text": "te du palais",
      "hyphenation_role": "HypPart1",
      "hyphen_candidate": true,
      "hyphen_join_with_next": true,
      "logical_join_candidate": "porte"
    },
    {
      "line_id": "TL_102",
      "prev_text": "Il s'approcha de la por-",
      "ocr_text": "te du palais",
      "next_text": "La garde était présente.",
      "hyphenation_role": "HypPart2",
      "hyphen_candidate": true,
      "hyphen_join_with_prev": true,
      "logical_join_candidate": "porte"
    }
  ]
}
```

Le LLM corrige chaque ligne pour ses erreurs OCR éventuelles, mais ne déplace aucun fragment d'une ligne à l'autre. C'est le Hyphenation Reconciler qui gère ensuite la reconstruction ALTO.

---

## OpenAI (`openai_provider.py`)

- Lister : `GET /v1/models` + allowlist préfixes (`gpt-4`, `gpt-3.5`, `o1`, `o3`, `o4`)
- Exclure : `instruct`, `embedding`, `whisper`, `tts`, `dall-e`, `moderation`, `realtime`, `audio`
- Générer : `POST /v1/chat/completions` avec `response_format.type = "json_schema"`

---

## Anthropic (`anthropic_provider.py`)

- Lister : `GET /v1/models` (headers: `x-api-key`, `anthropic-version: 2023-06-01`)
- Générer : `POST /v1/messages` avec `output_config.format.type = "json_schema"`
- Fallback si 400/422 : plain JSON

---

## Mistral (`mistral_provider.py`)

- Lister : `GET /v1/models`, filtrer `capabilities.completion_chat == true`
- Générer : `POST /v1/chat/completions` avec `response_format.type = "json_schema"`
- Fallback si 400/422 : `response_format.type = "json_object"`

---

## Google Gemini (`google_provider.py`)

- Lister : `GET .../v1beta/models?key={api_key}`, filtrer `generateContent` dans `supportedGenerationMethods`
- Exclure : `embed`, `aqa`, `attribute`
- Générer : `POST .../models/{model}:generateContent` avec `responseMimeType: "application/json"` et `responseSchema`
