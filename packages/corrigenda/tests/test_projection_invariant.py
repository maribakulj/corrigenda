"""The written artefact must SAY what the run decided (§9 projection).

The rewrite returns the per-line texts of the final tree its bytes were
serialized from (ADR-011 — no second parse of the output), and the
pipeline verifies them against the decided texts BEFORE the writer
persists anything: any word-level divergence fails the run — a
divergent artefact is corruption, not a degradation. Serialization
fidelity (tree → bytes) is lxml's contract; what the invariant guards
is the rewriter's tree diverging from the run's decisions.

Known, tolerated projection loss: ALTO/PAGE tokenize line text into
word elements, so runs of consecutive whitespace cannot survive the
round-trip. The invariant therefore compares in whitespace-run normal
form; exact-spacing accounting belongs to the loss policy, not here.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.core.editing import EditScript, ReplaceLine
from corrigenda.errors import ProjectionError
from corrigenda.formats.alto.adapter import AltoFormatAdapter
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        pass

    def write_trace(self, *, traces_payload):
        pass


class _CaptureWriter:
    def __init__(self) -> None:
        self.corrected: dict[str, bytes] = {}
        self.traces: list[str] = []

    def write_corrected(self, *, source_stem, xml_bytes):
        self.corrected[source_stem] = xml_bytes

    def write_trace(self, *, traces_payload):
        self.traces.append(traces_payload)


class _CorruptingAdapter:
    """Real ALTO adapter whose rewrite ends with one line's text altered —
    simulates a rewriter bug that puts text the run never decided into
    the tree it serializes."""

    def __init__(self) -> None:
        self._inner = AltoFormatAdapter()

    def rewrite_file(self, *args, **kwargs):
        result = self._inner.rewrite_file(*args, **kwargs)
        first = next(iter(sorted(result.texts)))
        return replace(
            result, texts={**result.texts, first: "XX" + result.texts[first]}
        )


class _LineDroppingAdapter:
    """Real ALTO adapter whose rewrite loses one line — simulates a
    rewrite that dropped a TextLine from the artefact."""

    def __init__(self) -> None:
        self._inner = AltoFormatAdapter()

    def rewrite_file(self, *args, **kwargs):
        result = self._inner.rewrite_file(*args, **kwargs)
        texts = dict(result.texts)
        texts.pop(next(iter(sorted(texts))))
        return replace(result, texts=texts)


def _pipeline(adapter, writer) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=_Null(),
        output_writer=writer,
        format_adapter=adapter,
        provider_name="rules",
        model="v1",
    )


@pytest.mark.asyncio
async def test_corrupted_rewrite_fails_the_run_and_persists_nothing() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _CaptureWriter()
    with pytest.raises(ProjectionError) as excinfo:
        await _pipeline(_CorruptingAdapter(), writer).run(
            document_manifest=doc,
            source_files={_SAMPLE.name: _SAMPLE},
        )
    # The error names the file and the diverging line.
    assert _SAMPLE.name in str(excinfo.value)
    assert "TL" in str(excinfo.value)
    # Nothing was promoted: a divergent artefact must never reach the writer.
    assert writer.corrected == {}


@pytest.mark.asyncio
async def test_dropped_line_fails_the_run() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _CaptureWriter()
    with pytest.raises(ProjectionError, match="missing"):
        await _pipeline(_LineDroppingAdapter(), writer).run(
            document_manifest=doc,
            source_files={_SAMPLE.name: _SAMPLE},
        )
    assert writer.corrected == {}


class _DoubleSpaceProducer:
    """Proposes a correction ALTO cannot represent exactly (consecutive
    spaces) — the documented, tolerated projection loss."""

    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    async def produce(self, payload, *, policy):
        first = payload.lines[0]
        return (
            EditScript(
                ops=[
                    ReplaceLine(
                        line_id=first.line_id,
                        text=first.ocr_text.replace(" ", "  ", 1),
                    )
                ]
            ),
            None,
        )


@pytest.mark.asyncio
async def test_whitespace_collapse_is_tolerated_not_fatal() -> None:
    """Word tokenization collapses whitespace runs; that is a known
    projection property of the formats, not corruption — the run
    succeeds and the artefact is written."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _CaptureWriter()
    pipeline = CorrectionPipeline(
        producer=_DoubleSpaceProducer(),
        observer=_Null(),
        output_writer=writer,
        provider_name="x",
        model="y",
    )
    await pipeline.run(document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE})
    assert writer.corrected, "the run must have written its artefact"


@pytest.mark.asyncio
async def test_healthy_run_passes_the_invariant() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    writer = _CaptureWriter()
    pipeline = CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=_Null(),
        output_writer=writer,
        provider_name="rules",
        model="v1",
    )
    await pipeline.run(document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE})
    assert writer.corrected
