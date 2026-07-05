"""OpenAI provider implementation."""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import call_llm, extract_chat_text, extract_usage, get_json
from app.schemas import ModelInfo, Usage

_BASE = "https://api.openai.com"

_ALLOWLIST_PREFIXES = ("gpt-4", "gpt-3.5", "o1", "o3", "o4")
_DENYLIST_PATTERNS = (
    "instruct",
    "embedding",
    "whisper",
    "tts",
    "dall-e",
    "moderation",
    "realtime",
    "audio",
)


def _keep_model(model_id: str) -> bool:
    mid = model_id.lower()
    if not any(mid.startswith(p) for p in _ALLOWLIST_PREFIXES):
        return False
    if any(d in mid for d in _DENYLIST_PATTERNS):
        return False
    return True


class OpenAIProvider:
    async def list_models(self, api_key: str) -> list[ModelInfo]:
        data = await get_json(
            url=f"{_BASE}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if _keep_model(mid):
                models.append(ModelInfo(id=mid, label=mid))
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
    ) -> tuple[dict[str, Any], Usage | None]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": json_schema,
            },
        }

        data = await call_llm(url=f"{_BASE}/v1/chat/completions", headers=headers, body=body)
        return extract_chat_text(data, "OpenAI"), extract_usage(data)
