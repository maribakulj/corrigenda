"""Anthropic provider implementation."""

from __future__ import annotations

import json
import re
from typing import Any

from app.providers.base import call_llm, extract_usage, get_json
from app.schemas import ModelInfo, Usage

_BASE = "https://api.anthropic.com"
_VERSION = "2023-06-01"

# L10/R5 — `max_tokens` was hardcoded to 8192. Claude Sonnet/Opus 4.x
# support up to 64k output tokens; long chunks (50+ lines) routinely
# exceed 8192 once correction text + JSON overhead is counted, leading
# to silent truncation → invalid JSON → mis-classified as
# `is_llm_output_error` → retried (same truncation) → fallback to OCR.
# Compute dynamically with a generous per-line budget; cap at the
# model-family ceiling.
_MAX_TOKENS_FLOOR = 8192
_TOKENS_PER_LINE_BUDGET = 200  # generous: ~150 chars of correction + JSON noise


def _model_output_cap(model: str) -> int:
    """The model's real max output-token ceiling (audit P1).

    ``list_models`` returns every model unfiltered, so a user can pick a
    Claude 3 / 3.5 model whose output cap (4096 / 8192) is BELOW the old
    hardcoded floor+ceiling (8192 / 64000). Requesting max_tokens above
    the model's cap is a hard HTTP 400 that kills the chunk. Derive a
    safe cap from the model id (conservative on anything unrecognised).
    """
    m = model.lower()
    # Claude 3.5 family — 8192 output tokens.
    if "claude-3-5" in m or "claude-3.5" in m:
        return 8192
    # Claude 3.7 — 64k. Audit-F14: this branch MUST precede the generic
    # 'claude-3' one ("claude-3-7-…" contains the substring "claude-3"),
    # otherwise 3.7 is silently capped at 4096 and long chunks truncate
    # into a retry storm.
    if "claude-3-7" in m or "claude-3.7" in m:
        return 64_000
    # Claude 3 family (haiku/sonnet/opus) — 4096.
    if "claude-3" in m:
        return 4096
    # Claude 4.x (sonnet-4, opus-4, etc.) — 64k.
    if "-4" in m or "claude-4" in m:
        return 64_000
    # Unknown / future model: the safe, universally-supported ceiling.
    return 8192


# Audit-F13 — model families that REJECT the `temperature` parameter
# with a hard 400: Anthropic removed sampling params on Opus 4.7/4.8 and
# Fable 5/Mythos 5, and Sonnet 5 rejects any NON-default value (our
# retry ramp 0.0/0.3/0.5 is always non-default, so omit for the family).
# `list_models` returns the catalog unfiltered, so these are fully
# selectable; sending temperature failed the whole run (the schema
# fallback only dropped tool_choice, and the second 400 was permanent).
# Substring notes: "sonnet-5" does not occur in "claude-3-5-sonnet-…"
# or "claude-sonnet-4-5". Unknown FUTURE families are covered by the
# generic strip-param-on-400 fallback in base.call_llm.
_NO_TEMPERATURE_MARKERS = ("fable", "mythos", "opus-4-7", "opus-4-8", "sonnet-5")


def _supports_temperature(model: str) -> bool:
    m = model.lower()
    return not any(marker in m for marker in _NO_TEMPERATURE_MARKERS)


def _compute_max_tokens(user_payload: dict[str, Any], model: str) -> int:
    """Return a max_tokens that scales with the number of input lines but
    NEVER exceeds the selected model's real output cap.

    Under-budgeting silently truncates the JSON tool-use block (a
    JSONDecodeError retry storm); over-budgeting is a hard 400. So we
    scale by line count, floor generously, then clamp to the model cap.
    """
    cap = _model_output_cap(model)
    floor = min(_MAX_TOKENS_FLOOR, cap)
    lines = user_payload.get("lines")
    estimated = len(lines) * _TOKENS_PER_LINE_BUDGET if isinstance(lines, list) else floor
    return max(floor, min(cap, estimated))


# L10/F9 — fallback path used `json.loads(text)` directly. If the model
# emitted prose around the JSON ("Here's the JSON:\n{...}") the parse
# crashed; the entire chunk then fell back to OCR even though the JSON
# was right there. Extract the OUTERMOST JSON object from the text
# before parsing.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _try_extract_json_from_prose(text: str) -> dict[str, Any]:
    """Parse JSON tolerantly from a possibly-prose-prefixed text block.

    Strategy: try a direct `json.loads` first (works for clean output);
    if that raises, find the first `{` and last `}` and try the
    enclosed substring. Returns the parsed dict on success; re-raises
    the underlying JSONDecodeError on definitive failure (the
    orchestrator's retry classifier expects JSONDecodeError /
    ValueError for retryable errors).
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall through to the prose-extraction heuristic.
        pass
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        raise json.JSONDecodeError("No JSON object found in Anthropic text block", text, 0)
    # The greedy `.*` between `{` and `}` captures the outermost braces,
    # which is what we want (a tool returning `{"lines": [...]}` even
    # when wrapped in ```json ... ``` fences).
    return json.loads(match.group(0))


class AnthropicProvider:
    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": _VERSION,
            "Content-Type": "application/json",
        }

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        data = await get_json(
            url=f"{_BASE}/v1/models",
            headers=self._headers(api_key),
        )

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
    ) -> tuple[dict[str, Any], Usage | None]:
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
            "max_tokens": _compute_max_tokens(user_payload, model),
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
        # Audit-F13 — only send temperature to families that accept it.
        if _supports_temperature(model):
            body["temperature"] = temperature
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

        return _extract_anthropic_payload(data, tool_name), extract_usage(data)


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

    # Fallback: first text block, parsed as JSON. Tolerant to prose
    # prefix/suffix ("Here's the JSON:\n{...}", "```json\n{...}\n```")
    # via `_try_extract_json_from_prose` — see L10/F9 docstring.
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                return _try_extract_json_from_prose(text)

    raise ValueError(
        f"Anthropic response has no usable block "
        f"(types={[b.get('type') for b in blocks if isinstance(b, dict)]})"
    )
