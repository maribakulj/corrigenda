"""should_abort cooperative cancellation (spec F10).

Pins:
  - a should_abort that fires raises CorrectionAborted;
  - no output is written when the run aborts (writer never called);
  - the default (no should_abort) runs to completion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corrigenda import CorrectionAborted, CorrectionError, CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _EchoProvider:
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
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in user_payload.get("lines", [])
            ]
        }, None


class _RecordingWriter:
    def __init__(self) -> None:
        self.corrected_calls = 0
        self.trace_calls = 0

    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None:
        self.corrected_calls += 1

    def write_trace(self, *, traces_payload: str) -> None:
        self.trace_calls += 1


class _NullObserver:
    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


async def _run(writer: _RecordingWriter, should_abort):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        provider=_EchoProvider(),
        observer=_NullObserver(),
        output_writer=writer,
    )
    return await pipeline.run(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={_SAMPLE.name: _SAMPLE},
        should_abort=should_abort,
    )


@pytest.mark.asyncio
async def test_abort_immediately_raises_and_writes_nothing():
    writer = _RecordingWriter()
    with pytest.raises(CorrectionAborted):
        await _run(writer, should_abort=lambda: True)
    # No output persisted on abort.
    assert writer.corrected_calls == 0
    assert writer.trace_calls == 0


@pytest.mark.asyncio
async def test_correction_aborted_is_a_correction_error():
    writer = _RecordingWriter()
    with pytest.raises(CorrectionError):
        await _run(writer, should_abort=lambda: True)


@pytest.mark.asyncio
async def test_no_abort_runs_to_completion_and_writes():
    writer = _RecordingWriter()
    result = await _run(writer, should_abort=None)
    assert result.total_chunks >= 1
    assert writer.corrected_calls >= 1
    assert writer.trace_calls == 1


@pytest.mark.asyncio
async def test_abort_after_first_probe_stops_before_writing():
    """A probe that returns True on its 2nd call (i.e. after the first
    page/chunk boundary) still aborts before outputs are written."""
    writer = _RecordingWriter()
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(CorrectionAborted):
        await _run(writer, should_abort=probe)
    assert writer.corrected_calls == 0
    assert writer.trace_calls == 0
