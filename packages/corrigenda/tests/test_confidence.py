"""ROADMAP V3 Phase 1 — ConfidencePolicy, HeuristicScorer and the
multi-component LineConfidence block on the report.

Doctrine under test: components keep their names (never one magic
number), the aggregation formula is identified, ``min`` is the
conservative default, and write_wc stays LOCKED until calibration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline, ConfidencePolicy, HeuristicScorer
from corrigenda.core.alignment import align_tokens
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.formats.page.parser import (
    build_document_manifest as build_page_manifest,
)

from tests._pipeline_harness import DictProvider

_NS = "http://www.loc.gov/standards/alto/ns-v3#"

_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="600" HEIGHT="800">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="600" HEIGHT="60">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="300" HEIGHT="30">
            <String ID="S1" CONTENT="la" WC="0.9" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="30"/>
            <SP HPOS="60" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S2" CONTENT="rnaison" WC="0.5" HPOS="70" VPOS="0" WIDTH="110" HEIGHT="30"/>
            <SP HPOS="180" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S3" CONTENT="blanche" WC="0.7" HPOS="190" VPOS="0" WIDTH="110" HEIGHT="30"/>
          </TextLine>
          <TextLine ID="L2" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30">
            <String ID="S4" CONTENT="propre" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


class _Null:
    def on_event(self, *a, **k):
        pass


def _run(tmp_path: Path, corrections: dict[str, str], policy: ConfidencePolicy | None):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider(corrections),
        api_key="k",
        model="m",
        observer=_Null(),
        confidence_policy=policy,
    )
    return pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )


def _outcome(result, line_id: str):
    return next(o for o in result.report.lines if o.line_id == line_id)


# ---------------------------------------------------------------------------
# HeuristicScorer
# ---------------------------------------------------------------------------


def _score(scorer: HeuristicScorer, source: str, final: str) -> float:
    return scorer.score_line(
        source_text=source,
        final_text=final,
        alignment=align_tokens(source.split(), final.split()),
    )


def test_heuristic_unchanged_is_certain():
    assert _score(HeuristicScorer(), "la maison", "la maison") == 1.0


def test_heuristic_known_confusion_scores_high():
    scorer = HeuristicScorer()
    assert _score(scorer, "la rnaison", "la maison") >= 0.95
    assert _score(scorer, "ſoleil", "soleil") >= 0.95


def test_heuristic_wholesale_replacement_scores_low():
    assert _score(HeuristicScorer(), "aaa", "zzz") < 0.5


def test_heuristic_insertions_drag_the_score_down():
    scorer = HeuristicScorer()
    plain = _score(scorer, "la rnaison", "la maison")
    padded = _score(scorer, "la rnaison", "la maison des champs")
    assert padded < plain


def test_heuristic_lexicon_backs_a_plausible_correction():
    no_lex = HeuristicScorer()
    with_lex = HeuristicScorer(lexicon={"vieille"})
    # 'vieifle' → 'vieille' is not a single tabled confusion; the
    # lexicon vouches for it.
    assert _score(with_lex, "vieifle", "vieille") >= 0.9
    assert _score(with_lex, "vieifle", "vieille") >= _score(
        no_lex, "vieifle", "vieille"
    )


# ---------------------------------------------------------------------------
# Policy + report block
# ---------------------------------------------------------------------------


def test_default_policy_drop_reports_no_confidence(tmp_path: Path):
    result = _run(tmp_path, {"L1": "la maison blanche"}, None)
    assert all(o.confidence is None for o in result.report.lines)


def test_report_only_fills_the_multi_component_block(tmp_path: Path):
    result = _run(
        tmp_path,
        {"L1": "la maison blanche"},
        ConfidencePolicy(mode="report_only"),
    )
    conf = _outcome(result, "L1").confidence
    assert conf is not None
    # Every component keeps its name.
    assert conf.ocr == pytest.approx((0.9 + 0.5 + 0.7) / 3)
    assert conf.alignment is not None and conf.alignment > 0.9
    assert "heuristic" in conf.scorers and conf.scorers["heuristic"] >= 0.95
    assert conf.producer is None  # LLM uncertainty channel not landed yet
    # Identified conservative aggregation: min over present components —
    # here the source OCR confidence is the weakest evidence.
    assert conf.formula == "min"
    assert conf.decision == pytest.approx(conf.ocr)


def test_unchanged_line_without_wc_scores_certain(tmp_path: Path):
    """L2 has no WC attributes and stays unchanged: alignment and
    heuristic are 1.0, no OCR component — decision 1.0."""
    result = _run(
        tmp_path,
        {"L1": "la maison blanche"},
        ConfidencePolicy(mode="report_only"),
    )
    conf = _outcome(result, "L2").confidence
    assert conf is not None
    assert conf.ocr is None
    assert conf.decision == 1.0


def test_write_wc_is_locked_until_calibration():
    with pytest.raises(ValueError, match="locked until"):
        ConfidencePolicy(mode="write_wc")


def test_confidence_policy_stays_out_of_the_composite_fingerprint(tmp_path: Path):
    """report_only never touches the corrected XML, so it must NOT move
    the §11 stamp (the policy joins the composite when write_wc
    unlocks)."""

    class _Noop:
        wants_geometry = False
        wants_image = False

        async def produce(self, payload, *, options):  # pragma: no cover
            raise NotImplementedError

        def on_event(self, *a, **k):
            pass

    default = CorrectionPipeline(producer=_Noop(), observer=_Noop())
    scored = CorrectionPipeline(
        producer=_Noop(),
        observer=_Noop(),
        confidence_policy=ConfidencePolicy(mode="report_only"),
    )
    assert default.config_fingerprint() == scored.config_fingerprint()


# ---------------------------------------------------------------------------
# Source-confidence extraction (parsers)
# ---------------------------------------------------------------------------


def test_alto_parser_preserves_mean_wc(tmp_path: Path):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    by_id = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    assert by_id["L1"].ocr_confidence == pytest.approx((0.9 + 0.5 + 0.7) / 3)
    assert by_id["L2"].ocr_confidence is None


def test_page_parser_preserves_line_conf(tmp_path: Path):
    page_xml = """<?xml version="1.0"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="1000">
    <TextRegion id="r1">
      <Coords points="0,0 1000,0 1000,100 0,100"/>
      <TextLine id="l1">
        <Coords points="0,0 1000,0 1000,50 0,50"/>
        <TextEquiv conf="0.42"><Unicode>du texte</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>"""
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(page_xml, encoding="utf-8")
    doc = build_page_manifest([(xml_path, xml_path.name)])
    [line] = [lm for p in doc.pages for lm in p.lines]
    assert line.ocr_confidence == pytest.approx(0.42)
