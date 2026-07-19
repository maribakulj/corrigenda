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

from corrigenda import CorrectionPipeline, __version__
from corrigenda.formats.alto.adapter import AltoFormatAdapter
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
    """The result's bytes ARE the adapter's rewrite — the same content a
    caller would get rewriting the decided manifest itself."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = _pipeline()
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    assert set(result.corrected_files) == {_SAMPLE.name}
    assert result.corrected_files[_SAMPLE.name].startswith(b"<?xml")
    # Parity with a direct rewrite of the post-run manifest under the
    # same provenance labels: the result carries the rewrite, not a copy
    # that could drift from it.
    rerendered = AltoFormatAdapter().rewrite_file(
        _SAMPLE,
        [p for p in doc.pages if p.source_file == _SAMPLE.name],
        "rules",
        "v1",
        lib_version=__version__,
        config_fingerprint=pipeline.config_fingerprint(),
    )
    assert result.corrected_files[_SAMPLE.name] == rerendered.xml_bytes


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
