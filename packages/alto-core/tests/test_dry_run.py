"""Dry-run (apply=False) and the public CorrectionReport (spec §9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alto_core import CorrectionPipeline, CorrectionReport
from alto_core.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _IdentityProvider:
    async def list_models(self, api_key: str) -> list[Any]:
        return []

    async def complete_structured(self, **kw) -> tuple[dict[str, Any], Any]:
        payload = kw["user_payload"]
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in payload.get("lines", [])
            ]
        }, None


class _RecordingWriter:
    def __init__(self) -> None:
        self.corrected = 0
        self.trace = 0

    def write_corrected(self, *, source_stem: str, xml_bytes: bytes) -> None:
        self.corrected += 1

    def write_trace(self, *, traces_payload: str) -> None:
        self.trace += 1


class _Null:
    def on_event(self, *a, **k):
        pass


async def _run(writer: _RecordingWriter, *, apply: bool):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        provider=_IdentityProvider(), observer=_Null(), output_writer=writer
    )
    return await pipeline.run(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={_SAMPLE.name: _SAMPLE},
        apply=apply,
    )


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_reports():
    writer = _RecordingWriter()
    result = await _run(writer, apply=False)
    # Writer never touched.
    assert writer.corrected == 0
    assert writer.trace == 0
    # But the report is fully populated, including the in-memory rewrite.
    assert isinstance(result.report, CorrectionReport)
    assert result.report.total_lines > 0
    assert result.report.lines
    assert all(ln.rewriter_path is not None for ln in result.report.lines)
    assert all(ln.output_alto_text is not None for ln in result.report.lines)


@pytest.mark.asyncio
async def test_apply_true_persists():
    writer = _RecordingWriter()
    result = await _run(writer, apply=True)
    assert writer.corrected >= 1
    assert writer.trace == 1
    assert result.report.report_version == "1.0"


@pytest.mark.asyncio
async def test_report_version_is_stable():
    writer = _RecordingWriter()
    result = await _run(writer, apply=False)
    assert result.report.report_version == "1.0"
    # round-trips through JSON with a stable schema
    dumped = result.report.model_dump_json()
    assert '"report_version":"1.0"' in dumped.replace(" ", "")
