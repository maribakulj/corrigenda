"""ADR-011 slice D (first half) — the result carries its artefacts.

The engine computes the corrected XML on every run; before this slice
the bytes were reachable ONLY through the injected ``OutputWriter``
(and thus not at all on a dry run). ``CorrectionResult.corrected_files``
makes the result the output, and ``result.write(dir)`` is the
caller-side persistence helper — the migration path for retiring the
writer/``apply=`` from the engine surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Capture:
    def __init__(self) -> None:
        self.corrected: dict[str, bytes] = {}
        self.traces: list[str] = []

    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        self.corrected[source_stem] = xml_bytes

    def write_trace(self, *, traces_payload):
        self.traces.append(traces_payload)


def _pipeline(writer) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=writer,
        output_writer=writer,
        provider_name="rules",
        model="v1",
    )


@pytest.mark.asyncio
async def test_result_carries_the_same_bytes_the_writer_got() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _Capture()
    result = await _pipeline(writer).run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    assert set(result.corrected_files) == {_SAMPLE.name}
    assert result.corrected_files[_SAMPLE.name] == writer.corrected[_SAMPLE.stem]


@pytest.mark.asyncio
async def test_dry_run_carries_artifacts_without_touching_the_writer() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _Capture()
    result = await _pipeline(writer).run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}, apply=False
    )
    assert writer.corrected == {}, "dry run must persist nothing"
    assert result.corrected_files[_SAMPLE.name].startswith(b"<?xml")


@pytest.mark.asyncio
async def test_result_write_persists_artifacts_and_report(tmp_path: Path) -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _Capture()
    result = await _pipeline(writer).run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}, apply=False
    )
    out = tmp_path / "outputs"
    written = result.write(out)
    assert (out / _SAMPLE.name).read_bytes() == result.corrected_files[_SAMPLE.name]
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["report_version"] == result.report.report_version
    assert payload["total_lines"] == result.report.total_lines
    assert set(written) == {out / _SAMPLE.name, out / "report.json"}
