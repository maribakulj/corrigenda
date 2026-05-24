"""Google Gemini provider implementation."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.providers.base import call_llm
from app.schemas import ModelInfo

_BASE = "https://generativelanguage.googleapis.com"
_EXCLUDE_KEYWORDS = ("embed", "aqa", "attribute")


def _keep_model(model: dict[str, Any]) -> bool:
    name: str = model.get("name", "")
    short = name.split("/")[-1].lower()
    if any(kw in short for kw in _EXCLUDE_KEYWORDS):
        return False
    methods = model.get("supportedGenerationMethods", [])
    return "generateContent" in methods


class GoogleProvider:
    async def list_models(self, api_key: str) -> list[ModelInfo]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_BASE}/v1beta/models",
                params={"key": api_key},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        models = []
        for m in data.get("models", []):
            if not _keep_model(m):
                continue
            name: str = m.get("name", "")
            mid = name.split("/")[-1] if "/" in name else name
            label = m.get("displayName") or mid
            models.append(ModelInfo(id=mid, label=label))
        models.sort(key=lambda m: m.id)
        return models

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        schema_body = json_schema.get("schema", json_schema)

        gen_config: dict[str, Any] = {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema_body,
        }
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}],
                }
            ],
            "generationConfig": gen_config,
        }
        fallback_body = {
            **body,
            "generationConfig": {k: v for k, v in gen_config.items() if k != "responseSchema"},
        }

        url = f"{_BASE}/v1beta/models/{model}:generateContent"
        data = await call_llm(
            url=url,
            headers={"Content-Type": "application/json"},
            body=body,
            fallback_body=fallback_body,
            params={"key": api_key},
        )

        candidates = data.get("candidates")
        if not candidates or not isinstance(candidates, list):
            raise ValueError(f"Gemini response missing 'candidates': {list(data.keys())}")
        parts = candidates[0].get("content", {}).get("parts")
        if not parts or not isinstance(parts, list):
            raise ValueError("Gemini response has no parts in candidates[0].content")
        text = parts[0].get("text")
        if not text:
            raise ValueError("Gemini response has empty text in parts[0]")
        return json.loads(text)
