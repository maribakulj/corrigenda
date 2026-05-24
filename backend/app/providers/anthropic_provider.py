"""Anthropic provider implementation."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.providers.base import call_llm
from app.schemas import ModelInfo

_BASE = "https://api.anthropic.com"
_VERSION = "2023-06-01"


class AnthropicProvider:
    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": _VERSION,
            "Content-Type": "application/json",
        }

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_BASE}/v1/models",
                headers=self._headers(api_key),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            label = m.get("display_name") or mid
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
        """Force a tool call to get a structured JSON response.

        Anthropic's Messages API does not have a ``response_format`` /
        ``output_config`` parameter. The supported way to get schema-validated
        JSON is to declare a single tool with ``input_schema`` and force the
        model to use it via ``tool_choice``. The reply arrives as a
        ``tool_use`` content block whose ``input`` is already a parsed dict.
        """
        schema_body = json_schema.get("schema", json_schema)
        tool_name = json_schema.get("name", "structured_output")

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 8192,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                }
            ],
            "tools": [
                {
                    "name": tool_name,
                    "description": "Return the structured OCR correction result.",
                    "input_schema": schema_body,
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        # Fallback: keep the tool definition but drop the forced choice. The
        # model may still emit a tool_use voluntarily; otherwise it falls
        # back to free text that we'll attempt to JSON-parse.
        fallback_body = {k: v for k, v in body.items() if k != "tool_choice"}

        data = await call_llm(
            url=f"{_BASE}/v1/messages",
            headers=self._headers(api_key),
            body=body,
            fallback_body=fallback_body,
        )

        return _extract_anthropic_payload(data, tool_name)


def _extract_anthropic_payload(
    data: dict[str, Any],
    tool_name: str,
) -> dict[str, Any]:
    """Pull structured JSON out of an Anthropic Messages response.

    Preference order:
      1. A ``tool_use`` block whose ``name`` matches the requested tool
         (the normal path when ``tool_choice`` is forced).
      2. The first ``text`` block parsed as JSON (fallback path).

    Robust against responses that include ``thinking`` or other auxiliary
    blocks before the payload block.
    """
    blocks = data.get("content")
    if not blocks or not isinstance(blocks, list):
        raise ValueError(f"Anthropic response missing 'content': {list(data.keys())}")

    # Preferred path: forced tool_use
    for block in blocks:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == tool_name
        ):
            inp = block.get("input")
            if isinstance(inp, dict):
                return inp

    # Fallback: first text block, parsed as JSON
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                return json.loads(text)

    raise ValueError(
        f"Anthropic response has no usable block "
        f"(types={[b.get('type') for b in blocks if isinstance(b, dict)]})"
    )
