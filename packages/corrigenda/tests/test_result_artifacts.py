"""ADR-011 slice D — the result carries its artefacts.

The engine computes the corrected XML on every run; slice D put the
bytes on ``CorrectionResult.corrected_files`` and slice D-fin retired
the injected ``OutputWriter``/``apply=`` from the engine surface, so the
result is now the ONLY output channel: ``result.write(dir)`` is the
caller-side persistence helper, and hosts with their own transaction
(the demo backend's staging writer) persist the same bytes themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Null:
    def on_event(self, *a, **k):
        pass


def _pipeline() -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=_Null(),
        provider_name="rules",
        model="v1",
    )


@pytest.mark.asyncio
async def test_result_carries_the_rewritten_bytes() -> None:
    """The result's bytes are the decided artefact: re-extracting the
    per-line texts from them (public round-trip helper) reproduces every
    decision's final text, in the formats' whitespace-normal form."""
    from corrigenda import extract_output_texts

    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    result = await _pipeline().run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    assert set(result.corrected_files) == {_SAMPLE.name}
    xml_bytes = result.corrected_files[_SAMPLE.name]
    assert xml_bytes.startswith(b"<?xml")
    decisions = result.decisions.decisions
    extracted = extract_output_texts(xml_bytes, {d.ref.line_id for d in decisions})
    for d in decisions:
        assert " ".join(extracted[d.ref.line_id].split()) == " ".join(
            d.final_text.split()
        )


@pytest.mark.asyncio
async def test_result_write_persists_artifacts_and_report(tmp_path: Path) -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    result = await _pipeline().run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    out = tmp_path / "outputs"
    written = result.write(out)
    assert (out / _SAMPLE.name).read_bytes() == result.corrected_files[_SAMPLE.name]
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["report_version"] == result.report.report_version
    assert payload["total_lines"] == result.report.total_lines
    assert set(written) == {out / _SAMPLE.name, out / "report.json"}
