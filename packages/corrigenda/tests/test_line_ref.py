"""ADR-009 — document-wide line lookups are keyed by ``LineRef``.

The engine used to key its document-wide state three different ways
(hand-built composite strings, raw tuples, bare ids). One frozen key
type makes a cross-page keying mistake a type error instead of a
runtime overwrite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.core.identity import LineRef, line_ref
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


def test_line_ref_is_a_frozen_hashable_value() -> None:
    a = LineRef(page_id="P1", line_id="L1")
    assert a == LineRef(page_id="P1", line_id="L1")
    assert a != LineRef(page_id="P2", line_id="L1"), (
        "the same bare line_id on another page is ANOTHER line"
    )
    assert len({a, LineRef(page_id="P1", line_id="L1")}) == 1
    with pytest.raises(AttributeError):
        a.line_id = "L2"  # type: ignore[misc]


def test_composite_string_keys_cannot_collide_by_construction() -> None:
    """The historical failure mode of hand-built string keys: ids that
    contain the separator produce equal strings for different lines.
    Structured keys make the collision unrepresentable."""
    tricky_a = LineRef(page_id="P1:2", line_id="L1")
    tricky_b = LineRef(page_id="P1", line_id="2:L1")
    assert f"{tricky_a.page_id}:{tricky_a.line_id}" == (
        f"{tricky_b.page_id}:{tricky_b.line_id}"
    ), "sanity: the flat encodings DO collide"
    assert tricky_a != tricky_b


@pytest.mark.asyncio
async def test_result_traces_are_keyed_by_line_ref() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=_Null(),
        output_writer=_Null(),
        provider_name="rules",
        model="v1",
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}, apply=False
    )
    assert result.traces, "the run must have traced its lines"
    for key, trace in result.traces.items():
        assert isinstance(key, LineRef)
        assert key == LineRef(page_id=trace.page_id, line_id=trace.line_id)
    # Every manifest line is reachable through its ref — no collisions,
    # no leftover composite-string keys.
    for page in doc.pages:
        for lm in page.lines:
            assert line_ref(lm) in result.traces
