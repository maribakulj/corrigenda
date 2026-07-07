"""PAGE XML rewriter tests (spec 6.2 P1/P3/P4/P5/P7)."""

from __future__ import annotations

from pathlib import Path

from corrigenda.formats.page.parser import build_document_manifest, parse_page_file
from corrigenda.formats.page.rewriter import (
    extract_output_texts,
    rewrite_page_file,
)

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples" / "page"
_NEWSEYE = _EXAMPLES / "newseye-fr" / "0250199004.xml"
_LAFAYETTE_CORR = (
    _EXAMPLES / "LaFayette1678_Cleves_btv1b8610820b_corrected_0011_page_corrected.xml"
)


# ---------------------------------------------------------------------------
# Identity round-trip (6.3 byte-stability of unmodified lines)
# ---------------------------------------------------------------------------


def test_identity_roundtrip_leaves_every_line_untouched():
    doc = build_document_manifest([(_NEWSEYE, _NEWSEYE.name)])
    all_ids = {lm.line_id for p in doc.pages for lm in p.lines}
    src = {lm.line_id: lm.ocr_text for p in doc.pages for lm in p.lines}
    xml, metrics, paths = rewrite_page_file(_NEWSEYE, doc.pages, "t", "m")
    assert metrics.untouched == len(all_ids)
    assert metrics.fast_path == 0 and metrics.slow_path == 0
    out = extract_output_texts(xml, all_ids)
    assert out == src
    assert all(v == "untouched" for v in paths.values())


# ---------------------------------------------------------------------------
# Synthetic fixture — @conf, alternatives, PlainText, multi-word (P3/P4)
# ---------------------------------------------------------------------------


_RICH = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 300,0 300,50 0,50"/>
      <TextLine id="ln1">
        <Coords points="0,0 300,0 300,20 0,20"/>
        <Word id="w1"><Coords points="0,0 90,0 90,20 0,20"/>
          <TextEquiv conf="0.5"><Unicode>helo</Unicode></TextEquiv></Word>
        <Word id="w2"><Coords points="100,0 200,0 200,20 100,20"/>
          <TextEquiv conf="0.9"><Unicode>wrld</Unicode></TextEquiv></Word>
        <TextEquiv index="1"><Unicode>ALT READING</Unicode></TextEquiv>
        <TextEquiv index="0" conf="0.7">
          <PlainText>helo wrld</PlainText>
          <Unicode>helo wrld</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>
"""


def _write(tmp_path: Path, xml: str, name: str = "rich.xml") -> Path:
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return p


def test_fast_path_updates_words_line_drops_conf_and_alternatives(tmp_path: Path):
    p = _write(tmp_path, _RICH)
    doc = build_document_manifest([(p, p.name)])
    lm = doc.pages[0].lines[0]
    assert lm.ocr_text == "helo wrld"
    lm.corrected_text = "hello world"  # same word count → fast path

    xml, metrics, paths = rewrite_page_file(p, doc.pages, "prov", "mdl")
    assert paths["ln1"] == "fast_path"
    assert metrics.fast_path == 1

    # P3 — alternative line TextEquiv removed; @conf dropped on line + words.
    assert metrics.alt_textequiv_dropped == 1  # the index=1 alternative
    assert metrics.conf_dropped >= 1
    text = xml.decode("utf-8")
    assert "ALT READING" not in text
    assert "conf=" not in text  # every stale conf gone
    # P3 — Unicode AND PlainText updated on the canonical line TextEquiv.
    assert text.count("hello world") >= 1
    assert "<PlainText>hello world</PlainText>" in text
    # P4 — words updated in place, Coords kept.
    assert "<Unicode>hello</Unicode>" in text and "<Unicode>world</Unicode>" in text
    assert 'points="0,0 90,0 90,20 0,20"' in text  # w1 geometry untouched (P1)

    # Re-extraction is canonical.
    out = extract_output_texts(xml, {"ln1"})
    assert out["ln1"] == "hello world"


def test_slow_path_drops_words_when_count_changes(tmp_path: Path):
    p = _write(tmp_path, _RICH)
    doc = build_document_manifest([(p, p.name)])
    doc.pages[0].lines[0].corrected_text = "hello brave new world"  # 4 != 2 words

    xml, metrics, paths = rewrite_page_file(p, doc.pages, "prov", "mdl")
    assert paths["ln1"] == "slow_path"
    assert metrics.slow_path == 1
    assert metrics.words_dropped == 2  # both Word elements removed
    text = xml.decode("utf-8")
    assert "<Word" not in text  # words gone; text lives at line level
    out = extract_output_texts(xml, {"ln1"})
    assert out["ln1"] == "hello brave new world"


def test_geometry_polygons_never_rewritten(tmp_path: Path):
    p = _write(tmp_path, _RICH)
    doc = build_document_manifest([(p, p.name)])
    doc.pages[0].lines[0].corrected_text = "hello world"
    xml, _m, _paths = rewrite_page_file(p, doc.pages, "p", "m")
    text = xml.decode("utf-8")
    # Region + line polygons preserved verbatim (P1).
    assert 'points="0,0 300,0 300,50 0,50"' in text
    assert 'points="0,0 300,0 300,20 0,20"' in text


# ---------------------------------------------------------------------------
# P5 — hyphen character preservation (E5 extended)
# ---------------------------------------------------------------------------


def test_hyphen_char_preserved_when_producer_normalises(tmp_path: Path):
    """A producer that rewrites ``appro¬`` as ``appro-`` must not win: the
    source ``¬`` is restored. Since that was the only change, the line ends
    up untouched."""
    doc = build_document_manifest([(_LAFAYETTE_CORR, _LAFAYETTE_CORR.name)])
    hid = None
    for lm in doc.pages[0].lines:
        if lm.ocr_text.endswith("¬"):
            lm.corrected_text = lm.ocr_text[:-1] + "-"
            hid = lm.line_id
            break
    assert hid is not None
    xml, _m, paths = rewrite_page_file(_LAFAYETTE_CORR, doc.pages, "p", "m")
    out = extract_output_texts(xml, {hid})
    assert out[hid].endswith("¬")
    assert paths[hid] == "untouched"


def test_hyphen_preserved_with_real_word_change(tmp_path: Path):
    """When the word before the hyphen genuinely changes, keep the change
    but still end in the source hyphen char."""
    xml_in = _RICH.replace(
        "<Unicode>helo wrld</Unicode></TextEquiv>",
        "<Unicode>helo wrld¬</Unicode></TextEquiv>",
    ).replace("<Unicode>wrld</Unicode>", "<Unicode>wrld¬</Unicode>")
    p = _write(tmp_path, xml_in, "hyph.xml")
    doc = build_document_manifest([(p, p.name)])
    lm = doc.pages[0].lines[0]
    assert lm.ocr_text.endswith("¬")
    lm.corrected_text = "hello world-"  # corrected word + normalised hyphen
    xml, _m, _paths = rewrite_page_file(p, doc.pages, "p", "m")
    out = extract_output_texts(xml, {"ln1"})
    assert out["ln1"] == "hello world¬"  # word fixed, ¬ restored


# ---------------------------------------------------------------------------
# P7 — provenance placement by schema version
# ---------------------------------------------------------------------------


def test_provenance_metadata_item_on_2019(tmp_path: Path):
    p = _write(tmp_path, _RICH)
    doc = build_document_manifest([(p, p.name)])
    doc.pages[0].lines[0].corrected_text = "hello world"
    xml, _m, _paths = rewrite_page_file(
        p,
        doc.pages,
        "openai",
        "gpt",
        lib_version="0.1.0a1",
        config_fingerprint="deadbeef",
    )
    text = xml.decode("utf-8")
    assert "MetadataItem" in text
    assert 'type="processingStep"' in text
    assert "corrigenda 0.1.0a1" in text and "deadbeef" in text


def test_provenance_comments_fallback_on_2013():
    """2013 schema has no MetadataItem slot → provenance goes to Comments."""
    doc = build_document_manifest([(_LAFAYETTE_CORR, _LAFAYETTE_CORR.name)])
    doc.pages[0].lines[0].corrected_text = "CHANGED"
    xml, _m, _paths = rewrite_page_file(_LAFAYETTE_CORR, doc.pages, "openai", "gpt")
    text = xml.decode("utf-8")
    assert "MetadataItem" not in text
    assert "Post-OCR correction via openai/gpt" in text


def test_output_reparses_and_is_deterministic(tmp_path: Path):
    p = _write(tmp_path, _RICH)
    doc1 = build_document_manifest([(p, p.name)])
    doc1.pages[0].lines[0].corrected_text = "hello world"
    a, _m1, _p1 = rewrite_page_file(p, doc1.pages, "p", "m")

    doc2 = build_document_manifest([(p, p.name)])
    doc2.pages[0].lines[0].corrected_text = "hello world"
    b, _m2, _p2 = rewrite_page_file(p, doc2.pages, "p", "m")
    assert a == b  # deterministic, no wall-clock timestamp
    # And it re-parses cleanly.
    reparsed = tmp_path / "again.xml"
    reparsed.write_bytes(a)
    pages, _root = parse_page_file(reparsed, reparsed.name)
    assert pages[0].lines[0].ocr_text == "hello world"
