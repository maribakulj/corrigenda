"""Audit-F wave 2 (2026-07-13) — provider cluster (F13-F17).

Each test pins one confirmed finding of docs/audit/AUDIT-2026-07-13.md
(fix plan: docs/audit/PLAN-CORRECTIONS.md, Vague 2). Every test was
written to FAIL on the pre-fix code and pass after.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers.anthropic_provider import AnthropicProvider, _model_output_cap
from app.providers.base import OUTPUT_JSON_SCHEMA, call_llm
from app.providers.google_provider import GoogleProvider
from app.providers.openai_provider import OpenAIProvider
from tests.test_providers import _make_response, _patched_shared_client, _PostCapture

# ---------------------------------------------------------------------------
# F14 — _model_output_cap branch order: claude-3-7 was unreachable
# (matched the 'claude-3' branch first → capped at 4096 instead of 64000)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-3-haiku-20240307", 4096),
        ("claude-3-opus-20240229", 4096),
        ("claude-3-5-sonnet-20240620", 8192),
        ("claude-3.5-haiku", 8192),
        ("claude-3-7-sonnet-20250219", 64_000),  # RED pre-fix: 4096
        ("claude-3.7-sonnet", 64_000),  # RED pre-fix: 4096
        ("claude-sonnet-4-5", 64_000),
        # Wave-2 review corrected this row: Opus 4.8's documented output
        # cap is 128k, not the generic 4.x 64k originally pinned here.
        ("claude-opus-4-8", 128_000),
        ("some-future-model", 8192),
    ],
)
def test_f14_model_output_cap_table(model: str, expected: int):
    assert _model_output_cap(model) == expected, model


# ---------------------------------------------------------------------------
# F13 — temperature must NOT be sent to the model families that reject
# it with a hard 400 (Fable 5 / Mythos, Opus 4.7/4.8, Sonnet 5)
# ---------------------------------------------------------------------------

_ANTHROPIC_TOOL_RESPONSE = {
    "content": [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "ocr_correction",
            "input": {"lines": [{"line_id": "L1", "corrected_text": "hi"}]},
        }
    ]
}


async def _anthropic_body_for(model: str) -> dict:
    capture = _PostCapture(_ANTHROPIC_TOOL_RESPONSE)
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=capture)
        await AnthropicProvider().complete_structured(
            api_key="fake",
            model=model,
            system_prompt="SYS",
            user_payload={"lines": [{"line_id": "L1", "ocr_text": "hi"}]},
            json_schema=OUTPUT_JSON_SCHEMA,
        )
    assert capture.last_body is not None
    return capture.last_body


@pytest.mark.parametrize(
    "model",
    [
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-5",
    ],
)
async def test_f13_temperature_omitted_for_rejecting_families(model: str):
    body = await _anthropic_body_for(model)
    assert "temperature" not in body, model


@pytest.mark.parametrize(
    "model",
    [
        "claude-3-5-sonnet-20240620",
        "claude-3-7-sonnet-20250219",
        "claude-haiku-4-5",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
    ],
)
async def test_f13_temperature_still_sent_to_accepting_families(model: str):
    body = await _anthropic_body_for(model)
    assert body.get("temperature") == 0.0, model


# ---------------------------------------------------------------------------
# F13/F15 — generic strip-param fallback in call_llm: a 400 whose error
# message cites an unsupported parameter present in the body is retried
# once WITHOUT that parameter (covers future unknown models)
# ---------------------------------------------------------------------------


class _SeqPost:
    """Returns queued responses in order, capturing each request body."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.bodies: list[dict] = []

    def __call__(self, url, **kwargs):
        self.bodies.append(kwargs.get("json"))
        return self._responses.pop(0)


def _anthropic_400_temperature() -> httpx.Response:
    return _make_response(
        400,
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "`temperature` is not supported on this model.",
            },
        },
    )


async def test_f13_call_llm_strips_cited_param_and_retries():
    ok = _make_response(200, {"ok": True})
    seq = _SeqPost([_anthropic_400_temperature(), ok])
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=seq)
        data = await call_llm(
            url="https://api.example/v1/messages",
            headers={},
            body={"model": "claude-next-99", "temperature": 0.3, "messages": []},
        )
    assert data == {"ok": True}
    assert len(seq.bodies) == 2
    assert "temperature" in seq.bodies[0]
    assert "temperature" not in seq.bodies[1], "retry must strip the cited param"
    assert seq.bodies[1]["model"] == "claude-next-99"


async def test_f13_call_llm_strip_fallback_composes_with_schema_fallback():
    """First 400 cites temperature → strip retry; second 400 (schema) →
    the provider's fallback_body (also stripped) is tried."""
    schema_400 = _make_response(
        400,
        {"error": {"message": "tool_choice is not permitted with this model"}},
    )
    ok = _make_response(200, {"ok": True})
    seq = _SeqPost([_anthropic_400_temperature(), schema_400, ok])
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=seq)
        data = await call_llm(
            url="https://api.example/v1/messages",
            headers={},
            body={"model": "m", "temperature": 0.3, "tool_choice": {"type": "tool"}},
            fallback_body={"model": "m", "temperature": 0.3},
        )
    assert data == {"ok": True}
    assert len(seq.bodies) == 3
    assert "temperature" not in seq.bodies[1]
    # The schema fallback body is used for the last attempt, ALSO stripped
    # of the param the vendor already told us it rejects.
    assert "tool_choice" not in seq.bodies[2]
    assert "temperature" not in seq.bodies[2]


async def test_f13_call_llm_400_without_cited_param_still_fails_permanently():
    """A 400 that does not cite any strippable param keeps today's
    behaviour: no infinite retries, ProviderPermanentError."""
    from corrigenda.core.protocols import ProviderPermanentError

    bad = _make_response(400, {"error": {"message": "unknown model"}})
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=_SeqPost([bad]))
        with pytest.raises(ProviderPermanentError):
            await call_llm(
                url="https://api.example/v1/messages",
                headers={},
                body={"model": "m", "temperature": 0.0},
            )


# ---------------------------------------------------------------------------
# F15 — OpenAI o-series reasoning models reject temperature=0.0
# ---------------------------------------------------------------------------

_OPENAI_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": json.dumps({"lines": []})}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


async def _openai_body_for(model: str) -> dict:
    capture = _PostCapture(_OPENAI_RESPONSE)
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=capture)
        await OpenAIProvider().complete_structured(
            api_key="fake",
            model=model,
            system_prompt="SYS",
            user_payload={"lines": []},
            json_schema=OUTPUT_JSON_SCHEMA,
        )
    assert capture.last_body is not None
    return capture.last_body


@pytest.mark.parametrize("model", ["o1", "o1-preview", "o3-mini", "o4-mini"])
async def test_f15_temperature_omitted_for_reasoning_models(model: str):
    body = await _openai_body_for(model)
    assert "temperature" not in body, model


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"])
async def test_f15_temperature_still_sent_to_gpt_models(model: str):
    body = await _openai_body_for(model)
    assert body.get("temperature") == 0.0, model


# ---------------------------------------------------------------------------
# F16 — Gemini list_models must follow nextPageToken
# ---------------------------------------------------------------------------


async def test_f16_google_list_models_paginates():
    page1 = {
        "models": [
            {
                "name": "models/gemini-1.5-pro",
                "supportedGenerationMethods": ["generateContent"],
            }
        ],
        "nextPageToken": "tok-2",
    }
    page2 = {
        "models": [
            {
                "name": "models/gemini-2.0-flash",
                "supportedGenerationMethods": ["generateContent"],
            }
        ],
    }

    calls: list[dict] = []

    async def fake_get_json(*, url, headers=None, params=None, timeout=15):
        calls.append({"url": url, "params": params})
        return page1 if len(calls) == 1 else page2

    from app.providers import google_provider as gp

    original = gp.get_json
    gp.get_json = fake_get_json  # type: ignore[assignment]
    try:
        models = await GoogleProvider().list_models("fake-key")
    finally:
        gp.get_json = original  # type: ignore[assignment]

    assert [m.id for m in models] == ["gemini-1.5-pro", "gemini-2.0-flash"]
    assert len(calls) == 2
    assert (calls[1]["params"] or {}).get("pageToken") == "tok-2"


async def test_f16_google_pagination_is_bounded():
    """A vendor bug that always returns nextPageToken must not loop
    forever — the pagination is bounded."""
    looping = {
        "models": [],
        "nextPageToken": "again",
    }
    calls = 0

    async def fake_get_json(*, url, headers=None, params=None, timeout=15):
        nonlocal calls
        calls += 1
        return looping

    from app.providers import google_provider as gp

    original = gp.get_json
    gp.get_json = fake_get_json  # type: ignore[assignment]
    try:
        await GoogleProvider().list_models("fake-key")
    finally:
        gp.get_json = original  # type: ignore[assignment]

    assert calls <= 10


# ---------------------------------------------------------------------------
# F17 — provider error detail must render the enum VALUE ('openai'),
# not the enum repr ('Provider.OPENAI')
# ---------------------------------------------------------------------------


async def test_f17_provider_error_detail_uses_enum_value(monkeypatch):
    from fastapi import HTTPException

    from app.api import providers as providers_api
    from app.schemas import ListModelsRequest

    class _Boom:
        async def list_models(self, api_key: str):
            raise RuntimeError("upstream said no")

    monkeypatch.setattr(providers_api, "get_provider", lambda p: _Boom())

    body = ListModelsRequest(provider="openai", api_key="sk-test")
    request = None  # the limiter decorator needs a Request in prod; call the
    # underlying function directly (it is what FastAPI wraps).
    with pytest.raises(HTTPException) as exc_info:
        await providers_api.list_models.__wrapped__(request, body)  # type: ignore[attr-defined]

    detail = exc_info.value.detail
    assert "Provider.OPENAI" not in detail, detail
    assert "(openai)" in detail, detail


# ---------------------------------------------------------------------------
# Wave-2 adversarial-review follow-ups.
#
# Review finding 1 (MAJOR) — the F14 cap table stopped at the 4.x
# generation: claude-fable-5 / claude-mythos-5 / claude-sonnet-5 fell to
# the 8192 default (their documented cap is 128k), and opus-4-7/4-8 were
# under-tabled at 64k (documented 128k). An 8192 ceiling on a dense PAGE
# chunk reproduces the exact truncation → JSONDecodeError → retry-storm
# class F14 was meant to eradicate, on the current flagship models.
# Docs: platform.claude.com models overview table.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-fable-5", 128_000),
        ("claude-mythos-5", 128_000),
        ("claude-sonnet-5", 128_000),
        ("claude-sonnet-5-20260301", 128_000),
        ("claude-opus-4-8", 128_000),
        ("claude-opus-4-7-20260115", 128_000),
        ("claude-haiku-4-5", 64_000),
        ("claude-sonnet-4-6", 64_000),
        # First-generation Claude 4 Opus caps at 32k — 64k risked a 400.
        ("claude-opus-4-1-20250805", 32_000),
        ("claude-opus-4-0", 32_000),
    ],
)
def test_review_w2_model_output_cap_current_generations(model: str, expected: int):
    assert _model_output_cap(model) == expected, model


# ---------------------------------------------------------------------------
# Review finding 2 (MAJOR) — Anthropic /v1/models is paginated
# (limit default 20, max 1000; after_id/has_more/last_id) exactly like
# the Gemini endpoint F16 fixed; the single unparameterised GET silently
# hid every model past page one.
# ---------------------------------------------------------------------------


def _anthropic_models_page(ids: list[str], *, has_more: bool) -> dict:
    return {
        "data": [{"id": i, "display_name": i} for i in ids],
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
        "has_more": has_more,
    }


async def test_review_w2_anthropic_list_models_paginates():
    pages = [
        _anthropic_models_page(["claude-fable-5", "claude-haiku-4-5"], has_more=True),
        _anthropic_models_page(["claude-sonnet-5"], has_more=False),
    ]
    calls: list[dict] = []

    async def fake_get_json(*, url, headers=None, params=None, timeout=15):
        calls.append({"url": url, "params": params})
        return pages[len(calls) - 1]

    from app.providers import anthropic_provider as ap

    original = ap.get_json
    ap.get_json = fake_get_json  # type: ignore[assignment]
    try:
        models = await AnthropicProvider().list_models("fake-key")
    finally:
        ap.get_json = original  # type: ignore[assignment]

    assert [m.id for m in models] == [
        "claude-fable-5",
        "claude-haiku-4-5",
        "claude-sonnet-5",
    ]
    assert len(calls) == 2
    # Big pages (the API allows limit up to 1000) …
    assert (calls[0]["params"] or {}).get("limit") == "1000"
    # … and the cursor follows last_id.
    assert (calls[1]["params"] or {}).get("after_id") == "claude-haiku-4-5"


async def test_review_w2_anthropic_pagination_is_bounded_and_warns(caplog):
    looping = _anthropic_models_page(["claude-x"], has_more=True)
    calls = 0

    async def fake_get_json(*, url, headers=None, params=None, timeout=15):
        nonlocal calls
        calls += 1
        return looping

    from app.providers import anthropic_provider as ap

    original = ap.get_json
    ap.get_json = fake_get_json  # type: ignore[assignment]
    try:
        with caplog.at_level("WARNING"):
            await AnthropicProvider().list_models("fake-key")
    finally:
        ap.get_json = original  # type: ignore[assignment]

    assert calls == 10
    # Review finding 4 twin — hitting the bound with more pages pending
    # must be LOUD, not silent.
    assert any("model list truncated" in r.message.lower() for r in caplog.records)


async def test_review_w2_google_pagination_truncation_warns(caplog):
    """Review finding 4 — the F16 bound silently dropped pages 11+."""
    looping = {"models": [], "nextPageToken": "again"}

    async def fake_get_json(*, url, headers=None, params=None, timeout=15):
        return looping

    from app.providers import google_provider as gp

    original = gp.get_json
    gp.get_json = fake_get_json  # type: ignore[assignment]
    try:
        with caplog.at_level("WARNING"):
            await GoogleProvider().list_models("fake-key")
    finally:
        gp.get_json = original  # type: ignore[assignment]

    assert any("model list truncated" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Review finding 5 — the strip fallback was snake_case-only while
# Gemini's request body is camelCase: a 400 citing `topK` with
# generationConfig.topK present stripped nothing.
# ---------------------------------------------------------------------------


async def test_review_w2_strip_handles_camelcase_gemini_params():
    bad = _make_response(
        400,
        {
            "error": {
                "code": 400,
                "message": "Invalid JSON payload: generationConfig.topK is not supported.",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    ok = _make_response(200, {"ok": True})
    seq = _SeqPost([bad, ok])
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=seq)
        data = await call_llm(
            url="https://example.com/v1beta/models/x:generateContent",
            headers={},
            body={
                "contents": [],
                "generationConfig": {"temperature": 0.0, "topK": 40},
            },
        )
    assert data == {"ok": True}
    assert len(seq.bodies) == 2
    retried = seq.bodies[1]
    assert "topK" not in retried.get("generationConfig", {}), retried
    # Only the cited param goes — temperature stays.
    assert retried["generationConfig"].get("temperature") == 0.0


async def test_review_w2_strip_handles_cross_notation_citation():
    """An error citing snake_case while the body carries camelCase (or
    vice versa) must still strip — the aliases are one group."""
    bad = _make_response(400, {"error": {"message": "top_k is not supported"}})
    ok = _make_response(200, {"ok": True})
    seq = _SeqPost([bad, ok])
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=seq)
        await call_llm(
            url="https://example.com/x",
            headers={},
            body={"generationConfig": {"topK": 40}},
        )
    assert "topK" not in seq.bodies[1].get("generationConfig", {}), seq.bodies[1]


# ---------------------------------------------------------------------------
# Review finding 6 — the gpt-5 family was absent from BOTH the
# allowlist (models invisible) and the no-temperature table (every chunk
# paid a wasted 400 + strip retry).
# ---------------------------------------------------------------------------


def test_review_w2_gpt5_family_is_listed():
    from app.providers.openai_provider import _keep_model

    assert _keep_model("gpt-5")
    assert _keep_model("gpt-5-mini")
    assert not _keep_model("gpt-5-audio")  # denylist still applies


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "GPT-5-2026-01-01"])
async def test_review_w2_gpt5_omits_temperature(model: str):
    capture = _PostCapture(
        {"choices": [{"message": {"content": json.dumps({"lines": []})}, "finish_reason": "stop"}]}
    )
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=capture)
        await OpenAIProvider().complete_structured(
            api_key="fake",
            model=model,
            system_prompt="SYS",
            user_payload={"lines": []},
            json_schema=OUTPUT_JSON_SCHEMA,
        )
    assert capture.last_body is not None
    assert "temperature" not in capture.last_body, model


# ---------------------------------------------------------------------------
# Review finding 3 — the retry temperature ramp (0.0/0.3/0.5) is
# silently a no-op for no-temperature families: the orchestrator thinks
# it diversifies retries while every request is byte-identical. The
# omission of a NON-default requested temperature must be visible in
# the logs.
# ---------------------------------------------------------------------------


async def test_review_w2_omitted_ramp_temperature_is_logged(caplog):
    capture = _PostCapture(_ANTHROPIC_TOOL_RESPONSE)
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=capture)
        with caplog.at_level("INFO"):
            await AnthropicProvider().complete_structured(
                api_key="fake",
                model="claude-fable-5",
                system_prompt="SYS",
                user_payload={"lines": [{"line_id": "L1", "ocr_text": "hi"}]},
                json_schema=OUTPUT_JSON_SCHEMA,
                temperature=0.3,  # a retry-ramp value
            )
    assert any(
        "temperature" in r.message.lower() and "omit" in r.message.lower() for r in caplog.records
    )


async def test_review_w2_default_temperature_omission_stays_quiet(caplog):
    """First attempts (temperature 0.0) must NOT spam the log."""
    capture = _PostCapture(_ANTHROPIC_TOOL_RESPONSE)
    with _patched_shared_client() as instance:
        instance.post = AsyncMock(side_effect=capture)
        with caplog.at_level("INFO"):
            await AnthropicProvider().complete_structured(
                api_key="fake",
                model="claude-fable-5",
                system_prompt="SYS",
                user_payload={"lines": [{"line_id": "L1", "ocr_text": "hi"}]},
                json_schema=OUTPUT_JSON_SCHEMA,
                temperature=0.0,
            )
    assert not any(
        "omit" in r.message.lower() and "temperature" in r.message.lower() for r in caplog.records
    )
