"""P3.5 — ProposalFeatures: the guard's metrics are computed once.

``check_line`` measures the proposal (similarity to source, to the
neighbours, length ratio) while deciding; those numbers used to die
with the call. They now ride ``AcceptanceResult.features`` → the
working trace → the report's decision stage, so a consumer reading
``report.json`` can see HOW CLOSE a proposal was without re-deriving
anything — and no second SequenceMatcher pass exists anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline, LineStatus
from corrigenda.core.guards import check_line
from corrigenda.formats.alto.parser import build_document_manifest

from tests._pipeline_harness import DictProvider

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Null:
    def on_event(self, *a, **k):
        pass


async def _run(corrections: dict[str, str]):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider(corrections),
        api_key="k",
        model="m",
        observer=_Null(),
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    return doc, result


def test_check_line_records_what_it_computed() -> None:
    src = "La France est grande"
    # Accepted small correction: source similarity + length ratio measured.
    r = check_line(src, "La France est grande.", "avant", "après")
    assert r.accepted
    assert r.features is not None
    assert r.features.source_similarity is not None
    assert 0.9 < r.features.source_similarity < 1.0
    assert r.features.length_ratio == round(21 / 20, 4)
    # Identity: 1.0 similarity by definition, nothing else measured.
    ident = check_line(src, src)
    assert ident.features is not None
    assert ident.features.source_similarity == 1.0
    assert ident.features.prev_similarity is None
    # A rejection carries the measurements that led to it.
    rej = check_line(src, "zzz qqq www xxx yyy")
    assert not rej.accepted
    assert rej.reason == "too_different_from_source"
    assert rej.features is not None
    assert rej.features.source_similarity is not None
    assert rej.features.source_similarity < 0.5


@pytest.mark.asyncio
async def test_features_reach_the_report_decision_stage() -> None:
    """Every line that went through per-line acceptance carries the
    guard's measurements; hyphen-unit members decide through the
    reconciler instead and stay (documented) feature-less."""
    from corrigenda import HyphenRole

    doc, result = await _run({})  # identity run
    role_by_id = {lm.line_id: lm.hyphen_role for page in doc.pages for lm in page.lines}
    plain = [
        ln for ln in result.report.lines if role_by_id[ln.line_id] is HyphenRole.NONE
    ]
    assert plain, "sample.xml must have non-hyphen lines"
    for outcome in plain:
        f = outcome.decision.features
        assert f is not None, f"{outcome.line_id}: no features on the decision"
        assert f.source_similarity == 1.0


@pytest.mark.asyncio
async def test_rejected_line_reports_the_measured_similarity() -> None:
    doc, result = await _run({"TL1": "zzz qqq www xxx yyy vvv"})
    outcome = next(ln for ln in result.report.lines if ln.line_id == "TL1")
    assert outcome.decision.status == LineStatus.FALLBACK.value
    assert outcome.decision.reason is not None
    assert outcome.decision.reason.code == "too_different_from_source"
    f = outcome.decision.features
    assert f is not None
    assert f.source_similarity is not None and f.source_similarity < 0.5
    assert f.length_ratio is not None
