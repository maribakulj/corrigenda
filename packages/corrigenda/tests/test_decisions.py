"""ADR-011 slices C+E — the immutable DecisionSet IS the run's outcome.

Since slice E the input manifest is never mutated, so the set on
``result.decisions`` is the only place a run's decisions exist; what
these tests pin is the materialization contract: document reading
order, faithful text/reason projection against the traces, terminality
enforcement, and immutability of the value itself.
"""

from __future__ import annotations

import dataclasses

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.core.decisions import derive_decision_set
from corrigenda.core.identity import LineRef
from corrigenda.core.schemas import LineStatus
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

from tests.test_unit_fallback_atomicity import _XPAGE_ALTO, _Null


def _doc(tmp_path):
    src = tmp_path / "xpage.xml"
    src.write_text(_XPAGE_ALTO, encoding="utf-8")
    return build_document_manifest([(src, src.name)]), src


@pytest.mark.asyncio
async def test_decision_set_mirrors_a_real_run(tmp_path) -> None:
    doc, src = _doc(tmp_path)
    pipeline = CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=_Null(),
        provider_name="rules",
        model="v1",
    )
    result = await pipeline.run(document_manifest=doc, source_files={src.name: src})

    decisions = result.decisions
    # Document reading order: pages in manifest order, lines in page order.
    assert [d.ref for d in decisions.decisions] == [
        LineRef(page_id=page.page_id, line_id=lm.line_id)
        for page in doc.pages
        for lm in page.lines
    ]
    # Faithful projection: source text mirrors the parse, the decided
    # text matches the trace's projection, every decision is terminal.
    for page in doc.pages:
        for lm in page.lines:
            d = decisions.by_ref[LineRef(page_id=lm.page_id, line_id=lm.line_id)]
            assert d.source_text == lm.ocr_text
            assert d.status in (LineStatus.CORRECTED, LineStatus.FALLBACK)
            trace = result.traces[d.ref]
            assert d.final_text == trace.projected_text
    # The caller's manifest carries none of it (slice E).
    for page in doc.pages:
        for lm in page.lines:
            assert lm.corrected_text is None
            assert lm.status is LineStatus.PENDING
    # The result's accounting came from this same view.
    assert result.fallback_lines == decisions.fallback_lines
    assert result.fallback_reasons == decisions.fallback_reason_counts()


@pytest.mark.asyncio
async def test_fallback_reason_travels_onto_the_decision(tmp_path) -> None:
    """A fallen line's decision carries the trace's reason; the counts
    aggregate by prefix exactly as the result reports them."""
    from tests.test_unit_fallback_atomicity import _FailsPages

    doc, src = _doc(tmp_path)
    from corrigenda.core.schemas import RetryPolicy

    pipeline = CorrectionPipeline(
        producer=_FailsPages({"L0", "L1"}),
        observer=_Null(),
        retry_policy=RetryPolicy(transient_backoff_base=0.0, output_backoff_base=0.0),
        provider_name="x",
        model="m",
    )
    result = await pipeline.run(document_manifest=doc, source_files={src.name: src})
    decisions = result.decisions
    fallen = [d for d in decisions.decisions if d.status is LineStatus.FALLBACK]
    assert fallen, "the failing chunk must have produced fallbacks"
    assert all(d.fallback_reason for d in fallen)
    assert all(d.final_text == d.source_text for d in fallen)
    assert "all_attempts_exhausted" in decisions.fallback_reason_counts()


def test_decisions_are_immutable(tmp_path) -> None:
    doc, _ = _doc(tmp_path)
    for page in doc.pages:
        for lm in page.lines:
            lm.status = LineStatus.FALLBACK
            lm.corrected_text = lm.ocr_text
    decisions = derive_decision_set(doc, {})
    d = decisions.decisions[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.final_text = "autre"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decisions.decisions = ()  # type: ignore[misc]
