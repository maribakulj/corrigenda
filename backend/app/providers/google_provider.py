"""Google Gemini provider implementation."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.providers.base import call_llm, extract_usage, get_json
from app.schemas import ModelInfo, Usage

logger = logging.getLogger(__name__)

_BASE = "https://generativelanguage.googleapis.com"
_EXCLUDE_KEYWORDS = ("embed", "aqa", "attribute")

# L10/B2 — Gemini supports both the ?key=... URL parameter and the
# x-goog-api-key request header. The URL form surfaces the key in
# every httpx.HTTPStatusError repr (which is then echoed by
# app/api/providers.py error handlers and emitted to logs by httpx).
# The header form keeps the key out of URL/query-string surfaces.
_API_KEY_HEADER = "x-goog-api-key"


def _auth_headers(api_key: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return headers with the Gemini API key set via the recommended
    header transport. Merges in any caller-provided extras (e.g.
    ``Content-Type`` for POSTs).
    """
    headers: dict[str, str] = {_API_KEY_HEADER: api_key}
    if extra:
        headers.update(extra)
    return headers


def _keep_model(model: dict[str, Any]) -> bool:
    name: str = model.get("name", "")
    short = name.split("/")[-1].lower()
    if any(kw in short for kw in _EXCLUDE_KEYWORDS):
        return False
    methods = model.get("supportedGenerationMethods", [])
    return "generateContent" in methods


class GoogleProvider:
    async def list_models(self, api_key: str) -> list[ModelInfo]:
        # Audit-F16 — the ListModels endpoint is PAGINATED (default page
        # size 50 + nextPageToken); reading only the first response
        # silently hid every model past page one. Follow the token with
        # a safety bound so a misbehaving vendor can't loop us forever.
        models = []
        page_token: str | None = None
        for _ in range(10):
            params = {"pageToken": page_token} if page_token else None
            data = await get_json(
                url=f"{_BASE}/v1beta/models",
                headers=_auth_headers(api_key),
                params=params,
            )
            for m in data.get("models", []):
                if not _keep_model(m):
                    continue
                name: str = m.get("name", "")
                mid = name.split("/")[-1] if "/" in name else name
                label = m.get("displayName") or mid
                models.append(ModelInfo(id=mid, label=label))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        if page_token:
            # Wave-2 review — hitting the safety bound with pages still
            # pending must be LOUD: models past the bound simply never
            # appear in the UI otherwise.
            logger.warning(
                "Gemini model list truncated at the 10-page safety bound "
                "(nextPageToken still present) — some models are not shown"
            )
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
            headers=_auth_headers(api_key, extra={"Content-Type": "application/json"}),
            body=body,
            fallback_body=fallback_body,
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
        return json.loads(text), extract_usage(data)
