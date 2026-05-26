"""Complement L4 retry-classification tests with finer invariants.

Vibrant-pascal's L4 wave (``test_orchestrator.py::
test_pipeline_classifies_*``) pins the three classification branches —
hyphen → backoff 0, transient HTTP → backoff = attempt*2, other
ValueError → backoff = attempt. This file pins three additional
invariants that L4 doesn't cover:

  1. **Temperature ramp** — successive attempts use 0.0 → 0.3 → 0.5,
     pinned at 0.0 as long as the hyphen-violation flag is set.
  2. **Second consecutive hyphen-violation** on the SAME chunk falls
     out of the special branch into the linear-backoff branch (because
     the per-chunk ``hyphen_violation`` latch only fires once).
  3. **Fallback warning message format** — the warning event emitted
     when retries exhaust carries the truncated sanitised error in a
     ``message`` field, not the fixed hyphen tag.

These were the three holes I caught when writing my own retry tests on
the pre-extraction codebase; they survived the alto-core extraction
because the classifier didn't change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alto_core.alto.parser import build_document_manifest
from alto_core.pipeline.validator import HyphenIntegrityError

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider, SSEEvent
from app.storage.output_writer import FilesystemOutputWriter

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# Scripted provider — records the temperature it receives on every call.
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    def __init__(self, script: list[Exception | None]) -> None:
        """``script[i]`` is the outcome for call i: an Exception to raise,
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


@pytest.fixture
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every ``asyncio.sleep(N)`` from the retry path."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def spy(duration: float, *args: Any, **kwargs: Any) -> Any:
        recorded.append(duration)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", spy)
    return recorded


async def _run_and_collect(
    tmp_path: Path,
    provider: _ScriptedProvider,
) -> tuple[list[SSEEvent], JobStore, str]:
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    queue = store.subscribe(job_id)

    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=provider,
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
# Invariant 1 — Temperature ramp without hyphen flag: 0.0 → 0.3 → 0.5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temperature_ramp_without_hyphen_flag(tmp_path: Path, sleep_calls: list[float]):
    """Three consecutive llm_output_error retries must use the
    documented temperature ramp 0.0 → 0.3 → 0.5. A bug in the ramp
    would let the LLM repeat the same deterministic mistake forever."""
    err = ValueError("Persistent JSON error")
    provider = _ScriptedProvider([err, err])
    _events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    # First chunk used 3 calls before giving up: attempts 1, 2, 3 with
    # temperatures 0.0, 0.3, 0.5. Subsequent chunks (after the first
    # fails) keep using 0.0 because the retry policy is per-chunk.
    assert len(provider.temperatures) >= 3
    assert provider.temperatures[0] == 0.0
    assert provider.temperatures[1] == 0.3
    assert provider.temperatures[2] == 0.5


@pytest.mark.asyncio
async def test_hyphen_violation_keeps_temperature_zero_on_retry(
    tmp_path: Path, sleep_calls: list[float]
):
    """After a hyphen violation, the next call must still use temp=0.0,
    NOT the normal ramp. The classifier pins temperature low to give
    the LLM the best chance of producing the same deterministic output
    minus the bug."""
    provider = _ScriptedProvider(
        [HyphenIntegrityError("hyphen_integrity_violation: drift detected")]
    )
    _events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    # First two complete_structured calls hit the first chunk:
    # attempt 1 (raised hyphen violation) and attempt 2 (success).
    assert len(provider.temperatures) >= 2
    assert provider.temperatures[0] == 0.0
    assert provider.temperatures[1] == 0.0


# ---------------------------------------------------------------------------
# Invariant 2 — Second hyphen violation falls into linear backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_hyphen_violation_falls_into_linear_backoff(
    tmp_path: Path, sleep_calls: list[float]
):
    """The hyphen-special-case only fires ONCE per chunk
    (``if is_hyphen_violation and not hyphen_violation``). A second
    consecutive hyphen-violation falls into the generic-ValueError
    path: backoff = attempt = 2, AND the error_tag is the sanitised
    message (not the fixed 'hyphen_integrity_violation' tag).

    Without this latch the LLM could trigger an infinite-zero-backoff
    storm on a deterministically broken prompt."""
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
    # Second retry tag is the sanitised message — NOT the fixed tag.
    assert retries_for_first[1].data["error"] != "hyphen_integrity_violation"
    assert "hyphen_integrity_violation" in retries_for_first[1].data["error"]


# ---------------------------------------------------------------------------
# Invariant 3 — Fallback warning message format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_warning_message_carries_sanitised_truncated_error(
    tmp_path: Path, sleep_calls: list[float]
):
    """When attempts exhaust, the warning event must carry a message
    of the form ``"Fallback to OCR source: <sanitised>"`` with the
    error truncated to 120 chars. The fixed sentinel
    ``hyphen_integrity_violation`` is RESERVED for the retry event's
    error tag — the warning never uses it."""
    err = ValueError("Persistent JSON error " + "X" * 500)
    provider = _ScriptedProvider([err, err, err])
    events, _store, _job_id = await _run_and_collect(tmp_path, provider)

    warnings = [e for e in events if e.event == "warning"]
    fallback_warnings = [
        w for w in warnings if "Fallback to OCR source" in w.data.get("message", "")
    ]
    assert fallback_warnings, "Expected at least one 'Fallback to OCR source' warning"

    for w in fallback_warnings:
        msg = w.data["message"]
        # Format guarantees
        assert msg.startswith("Fallback to OCR source: ")
        sanitised_part = msg[len("Fallback to OCR source: ") :]
        # Truncation guarantee — the sanitised part is capped at 120 chars
        assert len(sanitised_part) <= 120
