"""PAGE Slice-D coverage: P6 custom offsets, ⸗ hyphen, report losses, 6.3 parity."""

from __future__ import annotations

from pathlib import Path

from corrigenda.core.schemas import CorrectionReport, HyphenRole
from corrigenda.formats.page._custom import strip_offset_groups
from corrigenda.formats.page.parser import build_document_manifest, parse_page_file
from corrigenda.formats.page.rewriter import extract_output_texts, rewrite_page_file

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples" / "page"
_LAF_CORR = _EXAMPLES / "LaFayette1678_Cleves_btv1b8610820b_corrected_0011_page_corrected.xml"
_LAF_RAW = _EXAMPLES / "LaFayette1678_Cleves_btv1b8610820b_corrected_0011_page_raw.xml"


def _write(tmp_path: Path, xml: str, name: str = "f.xml") -> Path:
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# P6 — custom microformat
# ---------------------------------------------------------------------------


def test_strip_offset_groups_unit():
    assert strip_offset_groups("readingOrder {index:0;}") == (
        "readingOrder {index:0;}",
        0,
    )
    assert strip_offset_groups(
        "readingOrder {index:2;} textStyle {offset:5; length:3;}"
    ) == ("readingOrder {index:2;}", 1)
    assert strip_offset_groups("textStyle {offset:0; length:4;}") == ("", 1)


_CUSTOM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1" custom="readingOrder {index:0;}">
      <Coords points="0,0 300,0 300,50 0,50"/>
      <TextLine id="ln1" custom="readingOrder {index:0;} textStyle {offset:0; length:4;}">
        <Coords points="0,0 300,0 300,20 0,20"/>
        <Word id="w1" custom="readingOrder {index:0;} textStyle {offset:0; length:4;}">
          <Coords points="0,0 90,0 90,20 0,20"/>
          <TextEquiv><Unicode>helo</Unicode></TextEquiv></Word>
        <Word id="w2"><Coords points="100,0 200,0 200,20 100,20"/>
          <TextEquiv><Unicode>wrld</Unicode></TextEquiv></Word>
        <TextEquiv><Unicode>helo wrld</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>
"""


def test_custom_offset_groups_stripped_on_change(tmp_path: Path):
    p = _write(tmp_path, _CUSTOM_FIXTURE)
    doc = build_document_manifest([(p, p.name)])
    doc.pages[0].lines[0].corrected_text = "hello world"  # fast path (2==2)

    xml, metrics, _paths = rewrite_page_file(p, doc.pages, "prov", "mdl")
    text = xml.decode("utf-8")
    # Offset-anchored group gone from BOTH line and word; readingOrder kept.
    assert "textStyle" not in text
    assert text.count("readingOrder {index:0;}") >= 2  # line + word (+ region)
    assert metrics.custom_offset_stripped == 2  # line custom + w1 custom


def test_custom_untouched_line_keeps_offsets(tmp_path: Path):
    """An UNTOUCHED line's custom (incl. offset groups) is never perturbed."""
    p = _write(tmp_path, _CUSTOM_FIXTURE)
    doc = build_document_manifest([(p, p.name)])
    # No correction → identity → untouched.
    xml, metrics, paths = rewrite_page_file(p, doc.pages, "prov", "mdl")
    assert paths["ln1"] == "untouched"
    assert metrics.custom_offset_stripped == 0
    assert "textStyle {offset:0; length:4;}" in xml.decode("utf-8")


# ---------------------------------------------------------------------------
# P5 — ⸗ (U+2E17) Fraktur double-oblique hyphen
# ---------------------------------------------------------------------------


_FRAKTUR = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 300,0 300,80 0,80"/>
      <TextLine id="ln1"><Coords points="0,0 300,0 300,20 0,20"/>
        <TextEquiv><Unicode>Waſ⸗</Unicode></TextEquiv></TextLine>
      <TextLine id="ln2"><Coords points="0,30 300,30 300,50 0,50"/>
        <TextEquiv><Unicode>ſer</Unicode></TextEquiv></TextLine>
    </TextRegion>
  </Page>
</PcGts>
"""


def test_fraktur_double_oblique_detected_and_preserved(tmp_path: Path):
    p = _write(tmp_path, _FRAKTUR)
    doc = build_document_manifest([(p, p.name)])
    l1, l2 = doc.pages[0].lines
    assert l1.hyphen_role == HyphenRole.PART1
    assert l2.hyphen_role == HyphenRole.PART2
    # Producer tries to normalise ⸗ -> - ; source char must win (E5 extended).
    l1.corrected_text = "Waſ-"
    xml, _m, paths = rewrite_page_file(p, doc.pages, "prov", "mdl")
    out = extract_output_texts(xml, {"ln1"})
    assert out["ln1"].endswith("⸗")
    assert paths["ln1"] == "untouched"  # swap-only collapses to untouched


# ---------------------------------------------------------------------------
# CorrectionReport.format_losses (additive; report_version unchanged)
# ---------------------------------------------------------------------------


def test_metrics_as_losses_and_report_field(tmp_path: Path):
    p = _write(tmp_path, _CUSTOM_FIXTURE)
    doc = build_document_manifest([(p, p.name)])
    doc.pages[0].lines[0].corrected_text = "hello brave world"  # 3 != 2 -> slow

    _xml, metrics, _paths = rewrite_page_file(p, doc.pages, "prov", "mdl")
    losses = metrics.as_losses()
    assert losses["words_dropped"] == 2
    assert losses["custom_offset_stripped"] == 1  # line custom (words removed)
    assert "hyphen_preserved" not in losses  # zero counters omitted

    report = CorrectionReport(run_id="r1", total_lines=1, format_losses=losses)
    assert report.report_version == "1.0"  # additive field, no bump
    assert report.format_losses == losses
    dumped = report.model_dump()
    assert dumped["format_losses"]["words_dropped"] == 2


# ---------------------------------------------------------------------------
# 6.3 inter-format / cross-variant parity
# ---------------------------------------------------------------------------


def test_roles_equivalent_across_raw_and_corrected_variants():
    """The raw and corrected PAGE of the same page must detect the same
    hyphen structure (same PART1/PART2 positions) — the info exists in both."""
    raw = build_document_manifest([(_LAF_RAW, _LAF_RAW.name)])
    corr = build_document_manifest([(_LAF_CORR, _LAF_CORR.name)])
    raw_roles = [lm.hyphen_role for p in raw.pages for lm in p.lines]
    corr_roles = [lm.hyphen_role for p in corr.pages for lm in p.lines]
    assert raw_roles == corr_roles


def test_identity_rewrite_is_text_stable_on_corpus():
    doc = build_document_manifest([(_LAF_CORR, _LAF_CORR.name)])
    ids = {lm.line_id for p in doc.pages for lm in p.lines}
    src = {lm.line_id: lm.ocr_text for p in doc.pages for lm in p.lines}
    xml, metrics, _paths = rewrite_page_file(_LAF_CORR, doc.pages, "t", "m")
    assert metrics.untouched == len(ids)
    assert extract_output_texts(xml, ids) == src
