"""The ALTO slow path must REPORT the semantic String attributes it drops.

When a correction changes a line's word count the rewriter rebuilds its
``String`` children from scratch, recycling only ``ID``/``STYLEREFS``/
``STYLE`` (§6.1). Attributes like ``TAGREFS`` (links to structural/semantic
tags) and ``language`` are NOT invalidated by a spelling fix, yet they were
dropped silently — the rewrite called itself "lossless" while losing them.

Policy (agreed): keep the conservative whitelist (a re-segmented line can't
positionally re-attach a tag to the right new word, so we do NOT guess), but
COUNT every dropped semantic attribute into the loss report so "lossless"
stops being a lie. ``WC``/``CC`` (genuinely invalidated) and recomputed
geometry are NOT losses. The fast path edits in place and keeps these
attributes untouched — so only the slow path reports.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from corrigenda.formats.alto._ns import _detect_namespace
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.formats.alto.rewriter import rewrite_alto_file

_NS = "http://www.loc.gov/standards/alto/ns-v3#"

_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="600" HEIGHT="800">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="600" HEIGHT="30">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="300" HEIGHT="30">
            <String ID="S1" CONTENT="foo" TAGREFS="T1" language="fr" CUSTOM="vendor-data" WC="0.9" STYLE="bold" HPOS="0" VPOS="0" WIDTH="120" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _run(tmp_path: Path, corrected: str):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    by_id = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    by_id["L1"].corrected_text = corrected
    return rewrite_alto_file(xml_path, doc.pages, "test", "mock")


def test_slow_path_reports_dropped_semantic_attrs(tmp_path: Path):
    # "foo" -> "foo bar": word count 1 -> 2 → slow path (rebuild).
    result = _run(tmp_path, "foo bar")
    assert result.rewriter_paths["L1"] == "slow_path"

    # The dropped semantic attributes are now COUNTED, per line and aggregate.
    assert result.losses.get("tagrefs_dropped") == 1
    assert result.losses.get("language_dropped") == 1
    assert result.losses.get("custom_dropped") == 1
    assert result.losses_by_line["L1"].get("tagrefs_dropped") == 1

    # WC (genuinely invalidated) and geometry/whitelist are NOT losses.
    assert "wc_dropped" not in result.losses
    assert "style_dropped" not in result.losses
    assert "hpos_dropped" not in result.losses
    assert "id_dropped" not in result.losses

    # The behaviour itself is unchanged: the attrs really are gone from output
    # (we report, we don't invent a re-attachment).
    root = etree.fromstring(result.xml_bytes)
    ns = _detect_namespace(root)
    strings = [
        s for s in root.iter(f"{{{ns}}}String") if s.get("CONTENT") in ("foo", "bar")
    ]
    assert all(s.get("TAGREFS") is None for s in strings)


def test_fast_path_preserves_and_reports_nothing(tmp_path: Path):
    # "foo" -> "bar": same word count → fast path edits CONTENT in place,
    # TAGREFS/language survive untouched, nothing to report.
    result = _run(tmp_path, "bar")
    assert result.rewriter_paths["L1"] == "fast_path"
    assert result.losses == {}
    assert result.losses_by_line == {}
    root = etree.fromstring(result.xml_bytes)
    ns = _detect_namespace(root)
    s1 = next(s for s in root.iter(f"{{{ns}}}String"))
    assert s1.get("TAGREFS") == "T1"
    assert s1.get("language") == "fr"


def test_untouched_line_reports_nothing(tmp_path: Path):
    result = _run(tmp_path, "foo")
    assert result.rewriter_paths["L1"] == "untouched"
    assert result.losses == {}


def test_loss_surfaces_in_correction_report(tmp_path: Path):
    """End-to-end: a slow-path attribute drop reaches the user-facing
    ``CorrectionReport.format_losses`` — the point of reporting it."""
    from corrigenda import CorrectionPipeline

    from tests._pipeline_harness import DictProvider, RecordingObserver

    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({"L1": "foo bar"}),
        api_key="k",
        model="m",
        provider_name="test",
        observer=RecordingObserver(),
    )
    result = pipeline.run_sync(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )
    assert result.report.format_losses is not None
    assert result.report.format_losses.get("tagrefs_dropped") == 1
    assert result.report.format_losses.get("language_dropped") == 1
