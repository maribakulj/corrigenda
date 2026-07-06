"""PAGE XML parser tests (spec 6.2 P1–P5, 6.3 parity).

Drives the real OCR17plus / NewsEye corpus plus small synthetic fixtures
for the features the corpus does not exercise (``@index`` alternatives,
the 2019 namespace).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda.core.schemas import HyphenRole
from corrigenda.formats.page._ns import (
    _namespace_year,
    polygon_to_bbox,
    supports_metadata_item,
)
from corrigenda.formats.page._text import canonical_line_text
from corrigenda.formats.page.parser import build_document_manifest, parse_page_file

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples" / "page"
_LAFAYETTE_CORR = _EXAMPLES / "LaFayette1678_Cleves_btv1b8610820b_corrected_0011_page_corrected.xml"
_LAFAYETTE_ALTO = _EXAMPLES / "LaFayette1678_Cleves_btv1b8610820b_corrected_0011_alto4.xml"
_DESCARTES_RAW = _EXAMPLES / "Descartes1637_Discours_btv1b86069594_corrected_0014_page_raw.xml"
_NEWSEYE = _EXAMPLES / "newseye-fr" / "0250199004.xml"


# ---------------------------------------------------------------------------
# _ns geometry + namespace helpers (P1, P7)
# ---------------------------------------------------------------------------


def test_polygon_to_bbox_basic():
    assert polygon_to_bbox("617,1046 3450,1046 3450,5797 617,5797") == (
        617,
        1046,
        3450 - 617,
        5797 - 1046,
    )


def test_polygon_to_bbox_tolerates_floats_and_junk():
    assert polygon_to_bbox("10.9,20.1 30,40 bad ,, 5,5") == (5, 5, 25, 35)


def test_polygon_to_bbox_empty_is_zero_box():
    assert polygon_to_bbox("") == (0, 0, 0, 0)
    assert polygon_to_bbox("   ") == (0, 0, 0, 0)


def test_namespace_year_and_metadata_item_support():
    ns13 = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15"
    ns19 = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"
    assert _namespace_year(ns13) == 2013
    assert _namespace_year(ns19) == 2019
    assert _namespace_year("urn:custom:page") is None
    assert not supports_metadata_item(ns13)
    assert supports_metadata_item(ns19)


# ---------------------------------------------------------------------------
# Corpus parse (structure, polygons, hyphenation)
# ---------------------------------------------------------------------------


def test_lafayette_corrected_structure_and_polygons():
    doc = build_document_manifest([(_LAFAYETTE_CORR, _LAFAYETTE_CORR.name)])
    assert doc.total_pages == 1
    assert doc.total_lines == 13
    line = doc.pages[0].lines[0]
    # P1 — polygon preserved verbatim, bbox derived.
    assert line.coords.polygon is not None
    assert "," in line.coords.polygon
    assert line.coords.width > 0 and line.coords.height > 0


def test_lafayette_p2_word_concat_fallback():
    """The corrected export has NO line-level TextEquiv — every line's text
    is the single Word's Unicode (P2 fallback). Text must still come through."""
    doc = build_document_manifest([(_LAFAYETTE_CORR, _LAFAYETTE_CORR.name)])
    texts = [lm.ocr_text for lm in doc.pages[0].lines]
    assert "AU LECTEUR." in texts
    assert "Velque appro¬" in texts  # ¬ preserved verbatim


def test_lafayette_hyphen_roles_heuristic_only():
    doc = build_document_manifest([(_LAFAYETTE_CORR, _LAFAYETTE_CORR.name)])
    part1 = [lm for lm in doc.pages[0].lines if lm.hyphen_role == HyphenRole.PART1]
    part2 = [lm for lm in doc.pages[0].lines if lm.hyphen_role == HyphenRole.PART2]
    assert len(part1) == 2 and len(part2) == 2
    # P5 — everything heuristic, never explicit, no SUBS content.
    for lm in doc.pages[0].lines:
        assert lm.hyphen_source_explicit is False
        assert lm.hyphen_subs_content is None
    # PART1 forward-linked to the following PART2.
    p1 = part1[0]
    assert p1.hyphen_pair_line_id is not None


def test_descartes_detects_chained_both():
    doc = build_document_manifest([(_DESCARTES_RAW, _DESCARTES_RAW.name)])
    both = [lm for lm in doc.pages[0].lines if lm.hyphen_role == HyphenRole.BOTH]
    assert len(both) == 1  # a run of three consecutive hyphenated lines


def test_newseye_columnar_press_parses():
    doc = build_document_manifest([(_NEWSEYE, _NEWSEYE.name)])
    assert doc.total_lines > 500
    assert doc.total_blocks > 100  # many columnar regions
    # Every line got a bbox from its polygon.
    assert all(
        lm.coords.polygon for p in doc.pages for lm in p.lines if lm.ocr_text
    )


# ---------------------------------------------------------------------------
# 6.3 inter-format parity: PAGE vs ALTO4 export of the SAME page
# ---------------------------------------------------------------------------


def test_lafayette_page_alto_text_parity_by_alignment():
    from corrigenda.formats.alto.parser import (
        build_document_manifest as alto_doc,
    )

    pd = build_document_manifest([(_LAFAYETTE_CORR, _LAFAYETTE_CORR.name)])
    ad = alto_doc([(_LAFAYETTE_ALTO, _LAFAYETTE_ALTO.name)])
    ptext = [lm.ocr_text for p in pd.pages for lm in p.lines]
    atext = [lm.ocr_text for p in ad.pages for lm in p.lines]
    # Segmentations differ by at most one line (catchword); every shared
    # line has byte-identical canonical text.
    shared = min(len(ptext), len(atext))
    assert abs(len(ptext) - len(atext)) <= 1
    assert ptext[:shared] == atext[:shared]


# ---------------------------------------------------------------------------
# Synthetic fixtures — P3 (@index) + 2019 namespace
# ---------------------------------------------------------------------------


_PAGE_2019_WITH_ALTERNATIVES = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 100,0 100,50 0,50"/>
      <TextLine id="ln1">
        <Coords points="0,0 100,0 100,20 0,20"/>
        <Word id="w1"><Coords points="0,0 40,0 40,20 0,20"/>
          <TextEquiv><Unicode>hello</Unicode></TextEquiv></Word>
        <Word id="w2"><Coords points="50,0 100,0 100,20 50,20"/>
          <TextEquiv><Unicode>world</Unicode></TextEquiv></Word>
        <TextEquiv index="2"><Unicode>WRONG ALTERNATIVE</Unicode></TextEquiv>
        <TextEquiv index="0"><Unicode>hello world</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>
"""


def test_p3_canonical_textequiv_picks_min_index(tmp_path: Path):
    p = tmp_path / "alt.xml"
    p.write_text(_PAGE_2019_WITH_ALTERNATIVES, encoding="utf-8")
    pages, _root = parse_page_file(p, p.name)
    line = pages[0].lines[0]
    # index=0 wins over index=2 (P3); NOT the word concat, NOT the alternative.
    assert line.ocr_text == "hello world"


def test_word_concat_used_when_no_line_textequiv(tmp_path: Path):
    xml = _PAGE_2019_WITH_ALTERNATIVES.replace(
        '        <TextEquiv index="2"><Unicode>WRONG ALTERNATIVE</Unicode></TextEquiv>\n',
        "",
    ).replace(
        '        <TextEquiv index="0"><Unicode>hello world</Unicode></TextEquiv>\n',
        "",
    )
    p = tmp_path / "noeq.xml"
    p.write_text(xml, encoding="utf-8")
    pages, _root = parse_page_file(p, p.name)
    assert pages[0].lines[0].ocr_text == "hello world"


@pytest.mark.parametrize("bad", ["", "notxml", "<PcGts></PcGts>"])
def test_parse_is_robust_to_degenerate_input(tmp_path: Path, bad: str):
    p = tmp_path / "bad.xml"
    p.write_text(bad, encoding="utf-8")
    try:
        pages, _root = parse_page_file(p, p.name)
    except Exception:
        return  # clean rejection acceptable
    assert isinstance(pages, list)
