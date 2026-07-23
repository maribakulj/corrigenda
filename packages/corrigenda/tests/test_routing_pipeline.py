"""ROADMAP V3 Phase 3 — routing wired into the pipeline (the economics).

A line the QE scorer + RoutingPolicy send to SKIP is confirmed clean
and NEVER reaches the producer — one LLM call not spent. Off by default
(no scorer / default policy), so every existing run is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corrigenda import (
    CorrectionPipeline,
    HeuristicQEScorer,
    LineRef,
    QEScorer,
    RoutingPolicy,
)
from corrigenda.formats.alto.parser import build_document_manifest

from tests._pipeline_harness import DictProvider

_NS = "http://www.loc.gov/standards/alto/ns-v3#"

# L1 is clean prose; L2 carries a digit-in-word OCR break (vil1e). With
# a skip band and the digit-signal scorer, L1 skips and L2 goes to the LLM.
_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="600" HEIGHT="800">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="600" HEIGHT="60">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="300" HEIGHT="30">
            <String ID="S1" CONTENT="la" HPOS="0" VPOS="0" WIDTH="90" HEIGHT="30"/>
            <SP HPOS="90" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S2" CONTENT="porte" HPOS="100" VPOS="0" WIDTH="200" HEIGHT="30"/>
          </TextLine>
          <TextLine ID="L2" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30">
            <String ID="S3" CONTENT="dans" HPOS="0" VPOS="30" WIDTH="120" HEIGHT="30"/>
            <SP HPOS="120" VPOS="30" WIDTH="10" HEIGHT="30"/>
            <String ID="S4" CONTENT="vil1e" HPOS="130" VPOS="30" WIDTH="170" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


class _CountingProvider(DictProvider):
    """A DictProvider that records which line_ids it was asked to correct."""

    def __init__(self, corrections: dict[str, str]) -> None:
        super().__init__(corrections)
        self.seen_targets: set[str] = set()
        self.calls = 0

    async def complete_structured(self, **kw: Any) -> tuple[dict[str, Any], None]:
        self.calls += 1
        for ln in kw["user_payload"].get("lines", []):
            self.seen_targets.add(ln["line_id"])
        return await super().complete_structured(**kw)


class _Null:
    def on_event(self, *a, **k):
        pass


def _run(provider: QEScorer, policy: RoutingPolicy | None, scorer, tmp_path: Path):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        provider,
        api_key="k",
        model="m",
        observer=_Null(),
        qe_scorer=scorer,
        routing_policy=policy,
    )
    return pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )


def test_skipped_line_is_not_a_target_so_its_output_is_discarded(tmp_path: Path):
    """In one page-granularity chunk a skipped line still rides as
    CONTEXT (its text is in the payload), but it is no longer a TARGET —
    so even a provider that tries to change it cannot: L1's final text
    stays its OCR. That is the skip taking effect within a live chunk."""
    # The provider "corrects" BOTH lines; only L2 (a real target) should
    # land — L1 is skipped, its output discarded.
    provider = _CountingProvider({"L1": "XXX changed", "L2": "dans ville"})
    scorer = HeuristicQEScorer()  # digit-in-word flags L2, not L1
    policy = RoutingPolicy(skip_at_or_below=0.0)  # QE==0 → skip
    result = _run(provider, policy, scorer, tmp_path)

    assert result.lines_skipped == 1
    d1 = result.decisions.by_ref[LineRef(page_id="P1", line_id="L1")]
    assert d1.status.value == "corrected"
    assert d1.final_text == "la porte"  # NOT "XXX changed" — output discarded
    d2 = result.decisions.by_ref[LineRef(page_id="P1", line_id="L2")]
    assert d2.final_text == "dans ville"  # a real target, corrected


def test_all_lines_skipped_means_zero_producer_calls(tmp_path: Path):
    provider = _CountingProvider({})
    # A scorer that judges everything clean; skip band catches all.
    policy = RoutingPolicy(skip_at_or_below=1.0)
    result = _run(provider, policy, HeuristicQEScorer(), tmp_path)
    assert provider.calls == 0
    assert result.lines_skipped == 2


def test_routing_off_by_default_sends_every_line(tmp_path: Path):
    """No scorer → no routing → historical behaviour, nothing skipped."""
    provider = _CountingProvider({})
    result = _run(provider, None, None, tmp_path)
    assert provider.seen_targets == {"L1", "L2"}
    assert result.lines_skipped == 0


def test_producer_calls_counted_and_routing_proves_cheaper(tmp_path: Path):
    """The cost signal (review: 'l'hybride doit prouver qu'il est moins
    cher'). Both lines here sit in ONE page chunk, so a partial skip
    can't drop the call — but skipping ALL of them does: routing-on
    makes strictly FEWER producer calls than routing-off."""
    off = _run(_CountingProvider({}), None, None, tmp_path)
    assert off.producer_calls >= 1  # the run actually called the producer

    # Route every line to SKIP → the only chunk is dropped → zero calls.
    skip_all = _run(
        _CountingProvider({}),
        RoutingPolicy(skip_at_or_below=1.0),
        HeuristicQEScorer(),
        tmp_path,
    )
    assert skip_all.producer_calls == 0
    assert skip_all.producer_calls < off.producer_calls
    assert skip_all.lines_skipped == 2
