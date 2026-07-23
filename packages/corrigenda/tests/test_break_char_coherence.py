"""P5 decision-side (ROADMAP V3 Phase 2) — decision == artefact, always.

Found by the very first oracle run over the OCR17+ real corpus: the
source line ends in ``-`` (raw Word OCR) while the reference ends in
``¬``; the PAGE rewriter's P5 pass forced the SOURCE character back
onto the corrected text AFTER the decision recorded ``¬``, so the
artefact diverged from the decision and ``_verify_projection``
(rightly) raised ``ProjectionError``. P5 now runs in the pipeline
BEFORE decisions materialize; the rewriter keeps its idempotent call
as defence in depth.
"""

from __future__ import annotations

from pathlib import Path

from corrigenda import CorrectionPipeline, LineRef
from corrigenda.core.pairing import preserve_break_char
from corrigenda.formats.loader import build_document_manifest

from tests._pipeline_harness import DictProvider

_PAGE = """<?xml version="1.0"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="1000">
    <TextRegion id="r1">
      <Coords points="0,0 1000,0 1000,200 0,200"/>
      <TextLine id="l1">
        <Coords points="0,0 1000,0 1000,50 0,50"/>
        <TextEquiv><Unicode>qu'il eft bon de les auoir tou-</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <Coords points="0,60 1000,60 1000,110 0,110"/>
        <TextEquiv><Unicode>jours devant les yeux</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>"""


class _Null:
    def on_event(self, *a, **k):
        pass


def test_preserve_break_char_is_pure_and_idempotent():
    assert preserve_break_char("...tou-", "...tou¬") == "...tou-"
    assert preserve_break_char("...tou¬", "...tou-") == "...tou¬"
    # No source break char, or no corrected break char → no-op.
    assert preserve_break_char("...tout", "...tou¬") == "...tou¬"
    assert preserve_break_char("...tou-", "...tout") == "...tout"
    # Idempotent.
    once = preserve_break_char("...tou-", "...tou¬")
    assert preserve_break_char("...tou-", once) == once


def test_decision_and_artefact_agree_on_the_source_break_char(tmp_path: Path):
    """Failed before the pipeline-side P5 pass: the run raised
    ProjectionError because the artefact carried '-' while the decision
    had recorded '¬'."""
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_PAGE, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({"l1": "qu'il eſt bon de les auoir tou¬"}),
        api_key="k",
        model="m",
        observer=_Null(),
    )
    result = pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )

    page_id = doc.pages[0].page_id
    decision = result.decisions.by_ref[LineRef(page_id=page_id, line_id="l1")]
    # The decision itself carries the SOURCE break character…
    assert decision.final_text.endswith("tou-")
    assert "eſt" in decision.final_text  # the real correction survived
    # …and the artefact agrees (no ProjectionError, same trailing char).
    out = result.corrected_files["p.xml"].decode("utf-8")
    assert "tou-" in out
    assert "tou¬" not in out
