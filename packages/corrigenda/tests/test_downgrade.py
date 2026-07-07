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

from corrigenda import CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch):
    """Skip the real retry back-off sleeps so these tests stay fast."""
    monkeypatch.setattr(
        "corrigenda.core.pipeline.asyncio.sleep",
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
    pipeline = CorrectionPipeline.for_provider(
        provider,
        api_key="k",
        model="m",
        observer=observer,
        output_writer=_NullWriter(),
    )
    return await pipeline.run(
        document_manifest=doc,
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


# ---------------------------------------------------------------------------
# F1 × F8 — the descent must re-plan TARGET lines only, never context lines
# ---------------------------------------------------------------------------

from corrigenda.core.schemas import (  # noqa: E402
    ChunkPlannerConfig,
    Coords,
    DocumentManifest,
    LineManifest,
    LineStatus,
    PageManifest,
    RetryPolicy,
)


def _mk_line(i: int) -> LineManifest:
    return LineManifest(
        line_id=f"L{i}",
        page_id="P1",
        block_id="B1",
        line_order_global=i,
        line_order_in_block=i,
        coords=Coords(hpos=0, vpos=i * 10, width=100, height=8),
        ocr_text=f"line {i}",
    )


def _mk_doc(n: int) -> DocumentManifest:
    lines = [_mk_line(i) for i in range(n)]
    page = PageManifest(
        page_id="P1",
        source_file="x.xml",
        page_index=0,
        page_width=100,
        page_height=1000,
        blocks=[],
        lines=lines,
    )
    return DocumentManifest(
        source_files=["x.xml"],
        pages=[page],
        total_pages=1,
        total_blocks=0,
        total_lines=n,
    )


class _WindowZeroFailProvider:
    """Fails every attempt of the WINDOW chunk starting at L0; succeeds
    everywhere else (including the LINE-grain descent). Records payloads."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def list_models(self, api_key: str) -> list[Any]:
        return []

    async def complete_structured(self, **kw: Any) -> tuple[dict[str, Any], Any]:
        payload = kw["user_payload"]
        self.payloads.append(payload)
        lines = payload.get("lines", [])
        if (
            payload.get("granularity") == "window"
            and lines
            and lines[0]["line_id"] == "L0"
        ):
            raise ValueError("mock failure on window 0")
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in lines
            ]
        }, None


class _NullWriter2:
    def write_corrected(self, **k: Any) -> None:
        pass

    def write_trace(self, **k: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_downgrade_replans_targets_only_never_context():
    """F1×F8 — window 0 [L0..L4] fails and downgrades; L4 is CONTEXT there
    (target of window 1). The LINE-grain descent must never own L4 — its
    correction belongs to window 1, where its context is maximal."""
    provider = _WindowZeroFailProvider()
    obs = _RecordingObserver()
    doc = _mk_doc(12)
    cfg = ChunkPlannerConfig(
        max_lines_per_request=5, line_window_size=5, line_window_overlap=1
    )
    pipeline = CorrectionPipeline.for_provider(
        provider,
        api_key="k",
        model="m",
        observer=obs,
        output_writer=_NullWriter2(),
        config=cfg,
        retry_policy=RetryPolicy(per_chunk_budget=12),
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={},
    )

    assert "chunk_downgraded" in obs.names()
    # The descent's LINE-grain payloads must not contain the context line L4.
    line_grain_ids = {
        ln["line_id"]
        for p in provider.payloads
        if p.get("granularity") == "line"
        for ln in p.get("lines", [])
    }
    assert line_grain_ids, "expected a LINE-grain descent"
    assert "L4" not in line_grain_ids, "context line stolen by the descent"
    # Every line still ends corrected (L4 by its own window), no fallback.
    assert result.fallback_count == 0
    for lm in doc.pages[0].lines:
        assert lm.status == LineStatus.CORRECTED, lm.line_id


# ---------------------------------------------------------------------------
# F1 × F10 — should_abort is honoured DURING the descent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_abort_fires_inside_descent():
    """Once the descent begins, a should_abort flip must raise
    CorrectionAborted from within it — not wait for the next top-level
    chunk — and must not be swallowed as a chunk_error event."""
    from corrigenda import CorrectionAborted

    provider = _WindowZeroFailProvider()
    obs = _RecordingObserver()
    doc = _mk_doc(12)
    cfg = ChunkPlannerConfig(
        max_lines_per_request=5, line_window_size=5, line_window_overlap=1
    )
    pipeline = CorrectionPipeline.for_provider(
        provider,
        api_key="k",
        model="m",
        observer=obs,
        output_writer=_NullWriter2(),
        config=cfg,
        retry_policy=RetryPolicy(per_chunk_budget=12),
    )

    def abort_once_downgraded() -> bool:
        return "chunk_downgraded" in obs.names()

    with pytest.raises(CorrectionAborted):
        await pipeline.run(
            document_manifest=doc,
            source_files={},
            should_abort=abort_once_downgraded,
        )
    # Not converted into a chunk_error.
    assert "chunk_error" not in obs.names()
