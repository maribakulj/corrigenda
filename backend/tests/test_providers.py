"""Tests for LLM providers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.providers import get_provider
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT
from app.providers.google_provider import GoogleProvider, _keep_model
from app.providers.mistral_provider import MistralProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.openai_provider import _keep_model as openai_keep
from app.schemas import Provider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://example.com"),
    )


# ---------------------------------------------------------------------------
# test_openai_allowlist_prefixes
# ---------------------------------------------------------------------------


def test_openai_allowlist_prefixes():
    assert openai_keep("gpt-4o")
    assert openai_keep("gpt-4-turbo")
    assert openai_keep("gpt-3.5-turbo")
    assert openai_keep("o1-preview")
    assert openai_keep("o3-mini")
    assert openai_keep("o4-mini")
    # Not in allowlist
    assert not openai_keep("babbage-002")
    assert not openai_keep("text-davinci-003")


# ---------------------------------------------------------------------------
# test_openai_denylist_patterns
# ---------------------------------------------------------------------------


def test_openai_denylist_patterns():
    assert not openai_keep("gpt-4-instruct")
    assert not openai_keep("gpt-4-embedding")
    assert not openai_keep("gpt-4-audio-preview")
    assert not openai_keep("gpt-4-realtime-preview")
    assert not openai_keep("gpt-4-tts")
    assert not openai_keep("dall-e-3")
    assert not openai_keep("whisper-1")
    assert not openai_keep("omni-moderation-latest")
    # Valid model not matched by denylist
    assert openai_keep("gpt-4o-mini")


# ---------------------------------------------------------------------------
# test_mistral_capability_filter
# ---------------------------------------------------------------------------


def test_mistral_capability_filter():
    models_data = {
        "data": [
            {
                "id": "mistral-large",
                "name": "Mistral Large",
                "capabilities": {"completion_chat": True},
            },
            {
                "id": "mistral-embed",
                "name": "Mistral Embed",
                "capabilities": {"completion_chat": False},
            },
            {
                "id": "mistral-small",
                "name": "Mistral Small",
                "capabilities": {"completion_chat": True},
            },
            {"id": "no-caps", "name": "No caps", "capabilities": {}},
        ]
    }

    # Use the filter logic directly
    kept = [
        m["id"]
        for m in models_data["data"]
        if m.get("capabilities", {}).get("completion_chat", False)
    ]
    assert "mistral-large" in kept
    assert "mistral-small" in kept
    assert "mistral-embed" not in kept
    assert "no-caps" not in kept


# ---------------------------------------------------------------------------
# test_google_generate_content_filter
# ---------------------------------------------------------------------------


def test_google_generate_content_filter():
    models = [
        {"name": "models/gemini-1.5-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-1.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/aqa", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/attribute-001", "supportedGenerationMethods": ["generateContent"]},
    ]
    kept = [m["name"].split("/")[-1] for m in models if _keep_model(m)]
    assert "gemini-1.5-pro" in kept
    assert "gemini-1.5-flash" in kept
    assert "text-embedding-004" not in kept
    assert "aqa" not in kept
    assert "attribute-001" not in kept


# ---------------------------------------------------------------------------
# test_anthropic_model_parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_model_parse():
    api_resp = {
        "data": [
            {"id": "claude-3-opus-20240229", "display_name": "Claude 3 Opus"},
            {"id": "claude-3-sonnet-20240229", "display_name": "Claude 3 Sonnet"},
        ]
    }

    mock_resp = _make_response(200, api_resp)

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=mock_resp)

        provider = AnthropicProvider()
        models = await provider.list_models("fake-key")

    ids = [m.id for m in models]
    labels = [m.label for m in models]
    assert "claude-3-opus-20240229" in ids
    assert "Claude 3 Opus" in labels
    assert "claude-3-sonnet-20240229" in ids


# ---------------------------------------------------------------------------
# test_system_prompt_contains_hyphen_rule
# ---------------------------------------------------------------------------


def test_system_prompt_contains_hyphen_rule():
    assert "HypPart1" in SYSTEM_PROMPT
    assert "HypPart2" in SYSTEM_PROMPT
    assert "13" in SYSTEM_PROMPT
    assert "backward_join_candidate" in SYSTEM_PROMPT
    assert "forward_join_candidate" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# test_get_provider_registry
# ---------------------------------------------------------------------------


def test_get_provider_registry():
    from app.providers.base import BaseProvider

    for p in Provider:
        provider = get_provider(p)
        assert isinstance(provider, BaseProvider)


# ---------------------------------------------------------------------------
# Anthropic complete_structured — uses tools API, not the inexistent
# `output_config` parameter (B-001) and handles multi-block responses (R-013).
# ---------------------------------------------------------------------------


class _PostCapture:
    """Captures the last httpx post() body for inspection in assertions.

    Synchronous __call__ — wrapped by AsyncMock(side_effect=...) which
    awaits the returned value automatically.
    """

    def __init__(self, response_body: dict) -> None:
        self.response_body = response_body
        self.last_body: dict | None = None

    def __call__(self, url, **kwargs):
        self.last_body = kwargs.get("json")
        return _make_response(200, self.response_body)


@pytest.mark.asyncio
async def test_anthropic_complete_structured_uses_tools_api():
    """Request body must declare a tool with input_schema and force tool_choice."""
    capture = _PostCapture(
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "ocr_correction",
                    "input": {"lines": [{"line_id": "L1", "corrected_text": "hi"}]},
                }
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)

        provider = AnthropicProvider()
        result = await provider.complete_structured(
            api_key="fake",
            model="claude-3-5-sonnet-20240620",
            system_prompt="SYS",
            user_payload={"lines": [{"line_id": "L1", "ocr_text": "hi"}]},
            json_schema=OUTPUT_JSON_SCHEMA,
        )

    # Result comes straight from tool_use.input — no JSON parse
    assert result == {"lines": [{"line_id": "L1", "corrected_text": "hi"}]}

    body = capture.last_body
    assert body is not None
    # Forbidden legacy keys
    assert "output_config" not in body
    assert "response_format" not in body
    # Required new keys
    assert "tools" in body and len(body["tools"]) == 1
    assert body["tools"][0]["name"] == "ocr_correction"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert body["tool_choice"] == {"type": "tool", "name": "ocr_correction"}


@pytest.mark.asyncio
async def test_anthropic_complete_structured_skips_thinking_block():
    """A thinking block before the tool_use must not be mistaken for the payload."""
    capture = _PostCapture(
        {
            "content": [
                {"type": "thinking", "thinking": "Let me consider..."},
                {
                    "type": "tool_use",
                    "id": "tu_2",
                    "name": "ocr_correction",
                    "input": {"lines": [{"line_id": "X", "corrected_text": "ok"}]},
                },
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = AnthropicProvider()
        result = await provider.complete_structured(
            api_key="fake",
            model="claude-x",
            system_prompt="SYS",
            user_payload={},
            json_schema=OUTPUT_JSON_SCHEMA,
        )

    assert result["lines"][0]["line_id"] == "X"


@pytest.mark.asyncio
async def test_anthropic_complete_structured_text_block_fallback():
    """When only a text block is returned (no tool_use), parse it as JSON."""
    capture = _PostCapture(
        {
            "content": [
                {"type": "text", "text": '{"lines":[{"line_id":"T","corrected_text":"y"}]}'},
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = AnthropicProvider()
        result = await provider.complete_structured(
            api_key="fake",
            model="claude-x",
            system_prompt="SYS",
            user_payload={},
            json_schema=OUTPUT_JSON_SCHEMA,
        )

    assert result == {"lines": [{"line_id": "T", "corrected_text": "y"}]}


@pytest.mark.asyncio
async def test_anthropic_complete_structured_no_usable_block_raises():
    """If neither tool_use nor text block is present, raise a descriptive error."""
    capture = _PostCapture(
        {
            "content": [
                {"type": "thinking", "thinking": "..."},
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = AnthropicProvider()
        with pytest.raises(ValueError, match="no usable block"):
            await provider.complete_structured(
                api_key="fake",
                model="claude-x",
                system_prompt="SYS",
                user_payload={},
                json_schema=OUTPUT_JSON_SCHEMA,
            )


# ---------------------------------------------------------------------------
# OpenAI complete_structured — request shape + chat-completions response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_complete_structured_uses_json_schema_response_format():
    capture = _PostCapture(
        {
            "choices": [
                {"message": {"content": '{"lines":[{"line_id":"L1","corrected_text":"hi"}]}'}}
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = OpenAIProvider()
        result = await provider.complete_structured(
            api_key="sk-fake",
            model="gpt-4o",
            system_prompt="SYS",
            user_payload={"lines": [{"line_id": "L1", "ocr_text": "hi"}]},
            json_schema=OUTPUT_JSON_SCHEMA,
        )

    assert result == {"lines": [{"line_id": "L1", "corrected_text": "hi"}]}
    body = capture.last_body
    assert body is not None
    assert body["model"] == "gpt-4o"
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": OUTPUT_JSON_SCHEMA,
    }
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"


@pytest.mark.asyncio
async def test_openai_complete_structured_raises_on_missing_choices():
    capture = _PostCapture({"object": "chat.completion"})  # no 'choices' key

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = OpenAIProvider()
        with pytest.raises(ValueError, match="missing 'choices'"):
            await provider.complete_structured(
                api_key="sk-fake",
                model="gpt-4o",
                system_prompt="SYS",
                user_payload={},
                json_schema=OUTPUT_JSON_SCHEMA,
            )


# ---------------------------------------------------------------------------
# Mistral complete_structured — body shape + fallback structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mistral_complete_structured_sends_json_schema():
    capture = _PostCapture(
        {"choices": [{"message": {"content": '{"lines":[{"line_id":"M","corrected_text":"y"}]}'}}]}
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = MistralProvider()
        result = await provider.complete_structured(
            api_key="key-fake",
            model="mistral-large",
            system_prompt="SYS",
            user_payload={"lines": [{"line_id": "M", "ocr_text": "y"}]},
            json_schema=OUTPUT_JSON_SCHEMA,
            temperature=0.3,
        )

    assert result == {"lines": [{"line_id": "M", "corrected_text": "y"}]}
    body = capture.last_body
    assert body["temperature"] == 0.3
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": OUTPUT_JSON_SCHEMA,
    }


# ---------------------------------------------------------------------------
# Google Gemini complete_structured — generationConfig + response extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_complete_structured_uses_response_schema():
    capture = _PostCapture(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": '{"lines":[{"line_id":"G","corrected_text":"k"}]}'}]
                    }
                }
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = GoogleProvider()
        result = await provider.complete_structured(
            api_key="AIza-fake",
            model="gemini-1.5-pro",
            system_prompt="SYS",
            user_payload={"lines": [{"line_id": "G", "ocr_text": "k"}]},
            json_schema=OUTPUT_JSON_SCHEMA,
        )

    assert result == {"lines": [{"line_id": "G", "corrected_text": "k"}]}
    body = capture.last_body
    gc = body["generationConfig"]
    assert gc["responseMimeType"] == "application/json"
    assert gc["responseSchema"] == OUTPUT_JSON_SCHEMA["schema"]
    assert "system_instruction" in body


@pytest.mark.asyncio
async def test_google_complete_structured_raises_on_missing_candidates():
    capture = _PostCapture({"promptFeedback": {"blockReason": "SAFETY"}})

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = GoogleProvider()
        with pytest.raises(ValueError, match="missing 'candidates'"):
            await provider.complete_structured(
                api_key="fake",
                model="gemini-x",
                system_prompt="SYS",
                user_payload={},
                json_schema=OUTPUT_JSON_SCHEMA,
            )


@pytest.mark.asyncio
async def test_google_complete_structured_raises_on_empty_parts():
    capture = _PostCapture({"candidates": [{"content": {"parts": []}}]})

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = GoogleProvider()
        with pytest.raises(ValueError, match="no parts"):
            await provider.complete_structured(
                api_key="fake",
                model="gemini-x",
                system_prompt="SYS",
                user_payload={},
                json_schema=OUTPUT_JSON_SCHEMA,
            )


# ---------------------------------------------------------------------------
# L10 / B2 — Google API key must NOT be sent as a URL query parameter.
#
# httpx.HTTPStatusError stringifies the failing request URL including
# its query string, so a `params={"key": SECRET}` call leaks the key
# into every error message — which `app/api/providers.py` echoes back
# to the client AND `httpx` writes to logs. Sending the key as an
# `x-goog-api-key` header keeps it out of URL/query-string surfaces.
# ---------------------------------------------------------------------------


_SECRET_KEY = "AIzaSyD-FAKE_TEST_SECRET_KEY_DO_NOT_LEAK"


class _FullKwargsCapture:
    """Captures every kwarg passed to httpx post/get for inspection."""

    def __init__(self, response_body: dict) -> None:
        self.response_body = response_body
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _make_response(200, self.response_body)


def _assert_key_not_in_url_or_params(call: dict, secret: str) -> None:
    url = str(call.get("url", ""))
    params = call.get("params") or {}
    params_str = " ".join(f"{k}={v}" for k, v in params.items())
    assert secret not in url, (
        f"API key leaked into request URL: {url!r}. "
        f"Use a header (x-goog-api-key) instead of params={{'key': ...}}."
    )
    assert secret not in params_str, (
        f"API key leaked into request params: {params_str!r}. "
        f"Use a header (x-goog-api-key) instead of params={{'key': ...}}."
    )


@pytest.mark.asyncio
async def test_google_list_models_does_not_leak_api_key_in_url():
    """L10/B2 — `GoogleProvider.list_models` must send the api_key as
    an HTTP header, NOT a URL query parameter. Pre-fix the call passed
    ``params={"key": api_key}`` which surfaced the key in every
    httpx.HTTPStatusError string representation, then echoed it back
    via ``app/api/providers.py`` error responses.
    """
    capture = _FullKwargsCapture({"models": []})

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=capture)
        provider = GoogleProvider()
        await provider.list_models(api_key=_SECRET_KEY)

    assert capture.calls, "GoogleProvider.list_models did not make any HTTP call"
    for call in capture.calls:
        _assert_key_not_in_url_or_params(call, _SECRET_KEY)
        headers = call.get("headers") or {}
        assert headers.get("x-goog-api-key") == _SECRET_KEY, (
            f"API key not sent via x-goog-api-key header: {headers!r}"
        )


@pytest.mark.asyncio
async def test_google_complete_structured_does_not_leak_api_key_in_url():
    """L10/B2 symmetric with list_models — the POST to
    ``:generateContent`` must also send the key via header, not URL.
    """
    capture = _FullKwargsCapture(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {"lines": [{"line_id": "L1", "corrected_text": "x"}]}
                                )
                            }
                        ]
                    }
                }
            ]
        }
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(side_effect=capture)
        provider = GoogleProvider()
        await provider.complete_structured(
            api_key=_SECRET_KEY,
            model="gemini-1.5-pro",
            system_prompt="SYS",
            user_payload={"lines": [{"line_id": "L1", "ocr_text": "x"}]},
            json_schema=OUTPUT_JSON_SCHEMA,
        )

    assert capture.calls, "GoogleProvider.complete_structured did not POST"
    for call in capture.calls:
        _assert_key_not_in_url_or_params(call, _SECRET_KEY)
        headers = call.get("headers") or {}
        assert headers.get("x-goog-api-key") == _SECRET_KEY, (
            f"API key not sent via x-goog-api-key header: {headers!r}"
        )


def test_google_provider_source_does_not_pass_api_key_via_params():
    """Source-AST contract — no call site in `google_provider.py` may
    pass ``params={"key": ...}`` (or any dict literal whose first key
    is ``"key"``). Catches accidental reintroduction of the URL-param
    form regardless of what the runtime tests cover.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "providers" / "google_provider.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "params":
                continue
            if not isinstance(kw.value, ast.Dict):
                continue
            for key in kw.value.keys:
                if isinstance(key, ast.Constant) and key.value == "key":
                    offenders.append((node.lineno, "params={'key': ...}"))
                    break
    assert not offenders, (
        f"google_provider.py still passes api_key via URL params: {offenders}. "
        f"Use headers={{'x-goog-api-key': api_key}} instead."
    )
