"""Granularity downgrade on retry-budget exhaustion (spec F1).

Pre-F1, a chunk that exhausted its retries reverted its *entire* line set
to OCR — at PAGE granularity, one malformed line cost the whole page. F1
re-plans a failed chunk's lines at the next-finer granularity and retries,
emitting ``chunk_downgraded``; only lines whose finest-grain chunk still
fails (or that exhaust the per-chunk budget) fall back to OCR.

Pins:
  - a transient burst of failures recovers via downgrade (no fallback),
    emitting ``chunk_downgraded``;
  - a persistent failure still ends in OCR fallback after downgrading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from alto_core import CorrectionPipeline
from alto_core.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch):
    """Skip the real retry back-off sleeps so these tests stay fast."""
    monkeypatch.setattr(
        "alto_core.pipeline.correction_pipeline.asyncio.sleep",
        AsyncMock(return_value=None),
    )


class _CountingProvider:
    """Raises ValueError for the first ``fail_times`` calls, then succeeds
    with an identity correction."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def list_models(self, api_key: str) -> list[Any]:
        return []

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], Any]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ValueError("mock malformed output")
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in user_payload.get("lines", [])
            ]
        }, None


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def on_event(self, event_type: Any, payload: dict[str, Any]) -> None:
        name = getattr(event_type, "value", str(event_type))
        self.events.append((name, payload))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


class _NullWriter:
    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None:
        pass

    def write_trace(self, *, traces_payload: str) -> None:
        pass


async def _run(provider: _CountingProvider, observer: _RecordingObserver):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        provider=provider,
        observer=observer,
        output_writer=_NullWriter(),
    )
    return await pipeline.run(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={_SAMPLE.name: _SAMPLE},
    )


@pytest.mark.asyncio
async def test_transient_failure_recovers_via_downgrade():
    """3 failures (one full attempt cap) then success: the chunk downgrades
    and recovers — no OCR fallback."""
    obs = _RecordingObserver()
    result = await _run(_CountingProvider(fail_times=3), obs)
    assert "chunk_downgraded" in obs.names()
    assert result.fallback_count == 0


@pytest.mark.asyncio
async def test_downgrade_event_carries_granularity_transition():
    obs = _RecordingObserver()
    await _run(_CountingProvider(fail_times=3), obs)
    downgrades = [p for n, p in obs.events if n == "chunk_downgraded"]
    assert downgrades
    d = downgrades[0]
    assert d["from_granularity"] != d["to_granularity"]
    assert "budget_remaining" in d


@pytest.mark.asyncio
async def test_persistent_failure_falls_back_after_downgrade():
    """Never-succeeding provider: downgrade is attempted but the per-chunk
    budget eventually forces the OCR fallback."""
    obs = _RecordingObserver()
    result = await _run(_CountingProvider(fail_times=9999), obs)
    assert "chunk_downgraded" in obs.names()
    assert result.fallback_count >= 1
    assert "warning" in obs.names()
