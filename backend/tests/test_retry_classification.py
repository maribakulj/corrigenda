"""Pin the exception-classification logic inside `_run_chunk` (audit §7.1).

`CorrectionPipeline._run_chunk` distinguishes three exception classes
by isinstance check:

  - HyphenIntegrityError (subclass of ValueError): retry with
    temperature=0.0, backoff 0, error_tag="hyphen_integrity_violation".
  - Anything NOT a ValueError (HTTP, runtime, …): retry with
    backoff = attempt * 2, error_tag = sanitized msg.
  - Other ValueError (schema validation, missing keys, …): retry with
    backoff = attempt, error_tag = sanitized msg.

Before Phase 3.2 the hyphen path was triggered by a string-match on
``"hyphen_integrity_violation" in str(exc)``. The migration to a typed
exception is meant to be observably equivalent in production (the
validator raises HyphenIntegrityError exactly where it used to embed
the magic string). These tests pin the OBSERVABLE behaviour: sleep
durations, retry event tags, temperature ramp.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.alto.parser import build_document_manifest
from app.jobs.orchestrator import run_job
from app.jobs.store import JobStore
from app.jobs.validator import HyphenIntegrityError
from app.schemas import ModelInfo, Provider, SSEEvent

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# A provider whose complete_structured returns a scripted sequence of
# (exception | success) outcomes, recording the temperature it receives
# on every call. Lets us pin both the retry strategy and what the LLM
# actually saw on each attempt.
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    def __init__(self, script: list[Exception | None]) -> None:
        """script[i] is the outcome for call i: an Exception to raise,
        or None to return an identity-correction success."""
        self.script = script
        self.temperatures: list[float] = []
        self._idx = 0

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        return [ModelInfo(id="mock", label="mock")]

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        self.temperatures.append(temperature)
        if self._idx < len(self.script):
            outcome = self.script[self._idx]
            self._idx += 1
            if outcome is not None:
                raise outcome
        return {
            "lines": [
                {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
                for line in user_payload.get("lines", [])
            ]
        }


# ---------------------------------------------------------------------------
# asyncio.sleep spy — captures durations without actually waiting.
# Patches the module's `asyncio` reference so production stays untouched
# in other test files.
# ---------------------------------------------------------------------------


@pytest.fixture
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def spy(duration: float, *args: Any, **kwargs: Any) -> Any:
        recorded.append(duration)
        # Yield control without actually waiting, so retries stay fast.
        await real_sleep(0)

    monkeypatch.setattr("app.jobs.correction_pipeline.asyncio.sleep", spy)
    return recorded


# ---------------------------------------------------------------------------
# Run a one-page job and return (events_by_type, provider).
# Uses the FIRST chunk of the sample for assertions — sample.xml fits in
# a single chunk per page given the default ChunkPlannerConfig.
# ---------------------------------------------------------------------------


async def _run_and_collect(
    tmp_path: Path,
    provider: _ScriptedProvider,
) -> tuple[list[SSEEvent], JobStore, str]:
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    queue = store.subscribe(job_id)

    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await run_job(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        output_dir=tmp_path,
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=provider,
        job_store_override=store,
    )

    events: list[SSEEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events, store, job_id


def _first_retry_for_first_chunk(events: list[SSEEvent]) -> SSEEvent | None:
    """Return the first 'retry' event scoped to the first chunk emitted."""
    first_chunk_id: str | None = None
    for ev in events:
        if ev.event == "chunk_started" and first_chunk_id is None:
            first_chunk_id = ev.data.get("chunk_id")
        if ev.event == "retry" and ev.data.get("chunk_id") == first_chunk_id:
            return ev
    return None


# ---------------------------------------------------------------------------
# Hyphen-integrity violation: backoff 0, error_tag fixed, temperature 0.0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hyphen_violation_uses_zero_backoff(tmp_path: Path, sleep_calls: list[float]):
    """First chunk gets hyphen violation on attempt 1 then succeeds.
    The retry must NOT sleep (backoff=0 → asyncio.sleep skipped)."""
    provider = _ScriptedProvider(
        [HyphenIntegrityError("hyphen_integrity_violation: PART1 grew from 3 to 8 words")]
    )
    events, store, job_id = await _run_and_collect(tmp_path, provider)

    job = store.get_job(job_id)
    assert job is not None and job.status.value == "completed"
    # No sleep recorded for the hyphen retry. sleep_calls may contain
    # entries from other chunks (none in sample.xml — single chunk per
    # page) but the hyphen path explicitly skips asyncio.sleep.
    assert sleep_calls == [], f"Hyphen retry must not sleep; got durations {sleep_calls}"


@pytest.mark.asyncio
async def test_hyphen_violation_retry_event_has_fixed_error_tag(
    tmp_path: Path, sleep_calls: list[float]
):
    """The retry event must carry error_tag='hyphen_integrity_violation'
    (not the truncated message). Phase 3 keeps this contract."""
    provider = _ScriptedProvider(
        [HyphenIntegrityError("hyphen_integrity_violation: anything goes after this")]
    )
    events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    retry = _first_retry_for_first_chunk(events)
    assert retry is not None, "Expected at least one retry event"
    assert retry.data.get("error") == "hyphen_integrity_violation"
    assert retry.data.get("attempt") == 1


@pytest.mark.asyncio
async def test_hyphen_violation_keeps_temperature_zero_on_retry(
    tmp_path: Path, sleep_calls: list[float]
):
    """After a hyphen violation, the next call must still use temp=0.0,
    not the normal ramp (0.0 → 0.3 → 0.5)."""
    provider = _ScriptedProvider(
        [HyphenIntegrityError("hyphen_integrity_violation: drift detected")]
    )
    events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    # First two complete_structured calls hit the first chunk:
    # attempt 1 (raised hyphen violation) and attempt 2 (success).
    assert len(provider.temperatures) >= 2
    assert provider.temperatures[0] == 0.0
    assert provider.temperatures[1] == 0.0


# ---------------------------------------------------------------------------
# HTTP / non-ValueError exception: backoff = attempt * 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_uses_exponential_backoff(tmp_path: Path, sleep_calls: list[float]):
    """A non-ValueError on attempt 1 triggers backoff = 1*2 = 2 seconds."""
    request = httpx.Request("POST", "https://api.example.com/v1/chat")
    response = httpx.Response(503, request=request)
    http_exc = httpx.HTTPStatusError("503 Service Unavailable", request=request, response=response)

    provider = _ScriptedProvider([http_exc])
    events, store, job_id = await _run_and_collect(tmp_path, provider)

    job = store.get_job(job_id)
    assert job is not None and job.status.value == "completed"
    assert sleep_calls == [2], (
        f"HTTP retry on attempt 1 must sleep 2s (attempt * 2); got {sleep_calls}"
    )


@pytest.mark.asyncio
async def test_http_error_on_attempt_2_uses_backoff_4(tmp_path: Path, sleep_calls: list[float]):
    """Two consecutive HTTP errors: backoffs 2 then 4 (attempt * 2)."""
    request = httpx.Request("POST", "https://api.example.com/v1/chat")
    response = httpx.Response(503, request=request)
    http_exc = httpx.HTTPStatusError("503", request=request, response=response)

    provider = _ScriptedProvider([http_exc, http_exc])
    _events, store, job_id = await _run_and_collect(tmp_path, provider)

    job = store.get_job(job_id)
    assert job is not None and job.status.value == "completed"
    assert sleep_calls == [2, 4], f"Two HTTP retries must sleep [2, 4]; got {sleep_calls}"


@pytest.mark.asyncio
async def test_http_error_temperature_ramps_normally(tmp_path: Path, sleep_calls: list[float]):
    """Without hyphen_violation flag, temperature follows 0.0 → 0.3 → 0.5."""
    request = httpx.Request("POST", "https://api.example.com/v1/chat")
    response = httpx.Response(503, request=request)
    http_exc = httpx.HTTPStatusError("503", request=request, response=response)

    provider = _ScriptedProvider([http_exc, http_exc])
    _events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    # First chunk used 3 calls: attempt 1, 2, 3 with temperatures 0.0, 0.3, 0.5
    assert len(provider.temperatures) >= 3
    assert provider.temperatures[0] == 0.0
    assert provider.temperatures[1] == 0.3
    assert provider.temperatures[2] == 0.5


# ---------------------------------------------------------------------------
# Other ValueError (schema/validator): backoff = attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_valueerror_uses_linear_backoff(tmp_path: Path, sleep_calls: list[float]):
    """A non-hyphen ValueError on attempt 1 sleeps `attempt` seconds (= 1s)."""
    provider = _ScriptedProvider([ValueError("Missing key 'lines' in LLM response")])
    _events, store, job_id = await _run_and_collect(tmp_path, provider)

    job = store.get_job(job_id)
    assert job is not None and job.status.value == "completed"
    assert sleep_calls == [1], (
        f"Non-hyphen ValueError must sleep `attempt` (1s) on attempt 1; got {sleep_calls}"
    )


@pytest.mark.asyncio
async def test_other_valueerror_carries_sanitized_message_in_retry_tag(
    tmp_path: Path, sleep_calls: list[float]
):
    """The retry event's `error` must be the sanitized message, truncated to 120 chars.
    The 'hyphen_integrity_violation' fixed-tag path is NOT taken."""
    provider = _ScriptedProvider([ValueError("Bearer sk-1234567890abcdef rejected")])
    events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    retry = _first_retry_for_first_chunk(events)
    assert retry is not None
    err = retry.data.get("error", "")
    # Sanitized: the bearer token is masked
    assert "sk-1234567890abcdef" not in err
    # Not the hyphen fixed tag
    assert err != "hyphen_integrity_violation"
    # Truncated to 120 chars max
    assert len(err) <= 120


# ---------------------------------------------------------------------------
# Sequence of hyphen + http: flag clears after one hyphen retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_hyphen_violation_falls_into_linear_backoff(
    tmp_path: Path, sleep_calls: list[float]
):
    """The hyphen-special-case only fires ONCE per chunk
    (`if is_hyphen_violation and not hyphen_violation`). A second
    consecutive hyphen-violation falls into the generic-ValueError path:
    backoff = attempt = 2, AND the error_tag is the sanitized message
    (not the fixed 'hyphen_integrity_violation' tag)."""
    hyp_exc = HyphenIntegrityError("hyphen_integrity_violation: PART2 collapsed")
    provider = _ScriptedProvider([hyp_exc, hyp_exc])
    events, store, job_id = await _run_and_collect(tmp_path, provider)

    job = store.get_job(job_id)
    assert job is not None and job.status.value == "completed"

    # First retry: hyphen path → no sleep.
    # Second retry: generic ValueError path → sleep `attempt` = 2.
    assert sleep_calls == [2], (
        f"Second hyphen violation must use linear backoff (2s); got {sleep_calls}"
    )

    # Two retry events on the same chunk, with different error tags.
    first_chunk_id = None
    retries_for_first: list[SSEEvent] = []
    for ev in events:
        if ev.event == "chunk_started" and first_chunk_id is None:
            first_chunk_id = ev.data.get("chunk_id")
        if ev.event == "retry" and ev.data.get("chunk_id") == first_chunk_id:
            retries_for_first.append(ev)

    assert len(retries_for_first) == 2
    assert retries_for_first[0].data["error"] == "hyphen_integrity_violation"
    # Second retry tag is the sanitized message — NOT the fixed tag
    assert retries_for_first[1].data["error"] != "hyphen_integrity_violation"
    assert "hyphen_integrity_violation" in retries_for_first[1].data["error"]


# ---------------------------------------------------------------------------
# Exhaustion → fallback (no retry event on the final attempt, just warning)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausting_attempts_emits_warning_then_completes(
    tmp_path: Path, sleep_calls: list[float]
):
    """3 failures in a row → fallback (no 4th retry, just warning event)."""
    err = ValueError("Persistent JSON error")
    provider = _ScriptedProvider([err, err, err])
    events, store, job_id = await _run_and_collect(tmp_path, provider)

    job = store.get_job(job_id)
    assert job is not None and job.status.value == "completed"
    assert job.fallbacks >= 1

    # Two sleeps (after attempts 1 and 2). Attempt 3 fails → no retry, fallback.
    assert sleep_calls == [1, 2]

    # A warning event was emitted for the fallback.
    warnings = [e for e in events if e.event == "warning"]
    assert warnings, "Expected at least one warning event on fallback"
    assert any("Fallback to OCR source" in w.data.get("message", "") for w in warnings)
