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


# Audit-F15 — OpenAI's o-series reasoning models only accept the DEFAULT
# temperature (1) and return a hard 400 for any explicit value (our ramp
# is 0.0/0.3/0.5). The allowlist advertises them, so omit the parameter
# for the family; unknown future families are covered by the generic
# strip-param-on-400 fallback in base.call_llm.
_NO_TEMPERATURE_PREFIXES = ("o1", "o3", "o4")


def _supports_temperature(model: str) -> bool:
    return not model.lower().startswith(_NO_TEMPERATURE_PREFIXES)


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
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": json_schema,
            },
        }
        # Audit-F15 — only send temperature to families that accept it.
        if _supports_temperature(model):
            body["temperature"] = temperature
        # Audit P2 — the allowlist admits gpt-4-0613 / gpt-3.5-turbo, which
        # do NOT support response_format:{type:'json_schema'} and return a
        # hard 400. Every other provider passes a fallback_body so a
        # schema-rejection degrades to json_object instead of killing the
        # chunk; OpenAI now does too (the system prompt already instructs
        # JSON-only output, so json_object is safe).
        fallback_body = {
            **body,
            "response_format": {"type": "json_object"},
        }

        data = await call_llm(
            url=f"{_BASE}/v1/chat/completions",
            headers=headers,
            body=body,
            fallback_body=fallback_body,
        )
        return extract_chat_text(data, "OpenAI"), extract_usage(data)
