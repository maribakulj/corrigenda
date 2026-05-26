"""Pin the SSE event-name contract shared with the frontend (audit §12.3).

`frontend/src/hooks/useJobStream.ts` hard-codes the set of event names
it listens to. Any event the backend emits but the frontend does not
list will be silently dropped on the client. This test makes that
synchronisation explicit.

Two complementary properties:
  1. The set of names emitted by the backend on representative runs
     must be a subset of the frontend's `EVENTS` list.
  2. The happy path must emit every event the frontend depends on for
     progress display (started, document_parsed, page_started,
     chunk_*, page_completed, completed).

If the backend introduces a new event without updating the frontend,
property 1 catches it. If the backend silently stops emitting an
expected event, property 2 catches it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.alto.parser import build_document_manifest
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider, SSEEvent

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"
FRONTEND_HOOK = (
    Path(__file__).parent.parent.parent / "frontend" / "src" / "hooks" / "useJobStream.ts"
)


# ---------------------------------------------------------------------------
# Extract the frontend EVENTS list at import time — single source of truth.
# ---------------------------------------------------------------------------


def _frontend_events() -> set[str]:
    """Parse the EVENTS array literal out of useJobStream.ts."""
    src = FRONTEND_HOOK.read_text(encoding="utf-8")
    # Match `const EVENTS = [ ... ]` across multiple lines.
    m = re.search(r"const\s+EVENTS\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert m is not None, "Could not locate EVENTS array in useJobStream.ts"
    items = re.findall(r"'([a-z_]+)'", m.group(1))
    return set(items)


FRONTEND_EVENTS: set[str] = _frontend_events()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _MockProvider:
    def __init__(self, fail_times: int = 0, invalid_json_times: int = 0) -> None:
        self._fail_times = fail_times
        self._invalid_json_times = invalid_json_times

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
        if self._invalid_json_times > 0:
            self._invalid_json_times -= 1
            return {"bad_key": []}
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ValueError("mock LLM error")
        return {
            "lines": [
                {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
                for line in user_payload.get("lines", [])
            ]
        }


async def _collect_events(tmp_path: Path, provider: _MockProvider) -> list[str]:
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
        output_dir=tmp_path,
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=provider,
    )

    events: list[str] = []
    while not queue.empty():
        ev: SSEEvent = queue.get_nowait()
        events.append(ev.event)
    return events


# ---------------------------------------------------------------------------
# Static check on the frontend list itself
# ---------------------------------------------------------------------------


def test_frontend_events_list_is_non_empty():
    """Sanity: the extraction logic actually found events."""
    assert len(FRONTEND_EVENTS) >= 5
    # A few well-known ones must be present in any reasonable list.
    assert {"started", "completed", "failed"}.issubset(FRONTEND_EVENTS)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_emits_only_events_in_frontend_contract(tmp_path: Path):
    """Backend must only emit names the frontend knows how to dispatch."""
    events = await _collect_events(tmp_path, _MockProvider())
    extra = set(events) - FRONTEND_EVENTS
    assert not extra, (
        f"Backend emits events absent from frontend EVENTS list: {extra}. "
        f"Update frontend/src/hooks/useJobStream.ts or rename the event."
    )


@pytest.mark.asyncio
async def test_happy_path_emits_expected_progress_events(tmp_path: Path):
    """The frontend's progress display depends on these specific events.
    If the backend stops emitting one of them, the UI stalls silently."""
    events = await _collect_events(tmp_path, _MockProvider())
    required = {
        "started",
        "document_parsed",
        "page_started",
        "chunk_planned",
        "chunk_started",
        "chunk_completed",
        "page_completed",
        "completed",
    }
    missing = required - set(events)
    assert not missing, f"Happy path missing expected events: {missing}"


@pytest.mark.asyncio
async def test_happy_path_ordering(tmp_path: Path):
    """`started` must precede `document_parsed`; `document_parsed` must
    precede any chunk event; `completed` must be last."""
    events = await _collect_events(tmp_path, _MockProvider())
    assert events.index("started") < events.index("document_parsed")
    assert events.index("document_parsed") < events.index("chunk_started")
    assert events[-1] == "completed"


# ---------------------------------------------------------------------------
# Failure-path events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_event_emitted_on_invalid_json(tmp_path: Path):
    """A single bad-JSON response triggers a `retry` event before completion."""
    events = await _collect_events(tmp_path, _MockProvider(invalid_json_times=1))
    assert "retry" in events
    assert "retry" in FRONTEND_EVENTS  # belt + braces
    assert events.index("retry") < events.index("completed")


@pytest.mark.asyncio
async def test_warning_event_emitted_on_fallback(tmp_path: Path):
    """Exhausting all retries triggers a `warning` and the job still completes."""
    events = await _collect_events(tmp_path, _MockProvider(fail_times=99))
    assert "warning" in events
    assert "warning" in FRONTEND_EVENTS
    # Job is reported as completed even after fallback (per current contract)
    assert events[-1] == "completed"


# ---------------------------------------------------------------------------
# stream_events synthetic events (keepalive, terminal replay)
# ---------------------------------------------------------------------------


def test_synthetic_stream_event_names_are_in_frontend_contract():
    """`JobStore.stream_events` synthesises `keepalive` plus a replay of
    the terminal `completed`/`failed` status. All three must be known
    to the frontend dispatcher."""
    assert "keepalive" in FRONTEND_EVENTS
    assert "completed" in FRONTEND_EVENTS
    assert "failed" in FRONTEND_EVENTS
