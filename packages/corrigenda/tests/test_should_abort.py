"""should_abort cooperative cancellation (spec F10).

Pins:
  - a should_abort that fires raises CorrectionAborted — no result (and
    therefore no output a caller could persist) is ever produced;
  - the default (no should_abort) runs to completion with artefacts.
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


class _NullObserver:
    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


async def _run(should_abort):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline.for_provider(
        _EchoProvider(),
        api_key="k",
        model="m",
        observer=_NullObserver(),
    )
    return await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
        should_abort=should_abort,
    )


@pytest.mark.asyncio
async def test_abort_immediately_raises_and_produces_no_result():
    with pytest.raises(CorrectionAborted):
        await _run(should_abort=lambda: True)


@pytest.mark.asyncio
async def test_correction_aborted_is_a_correction_error():
    with pytest.raises(CorrectionError):
        await _run(should_abort=lambda: True)


@pytest.mark.asyncio
async def test_no_abort_runs_to_completion_with_artifacts():
    result = await _run(should_abort=None)
    assert result.total_chunks >= 1
    assert result.corrected_files


@pytest.mark.asyncio
async def test_abort_after_first_probe_still_raises():
    """A probe that returns True on its 2nd call (i.e. after the first
    page/chunk boundary) still aborts the run — no result escapes."""
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(CorrectionAborted):
        await _run(should_abort=probe)
