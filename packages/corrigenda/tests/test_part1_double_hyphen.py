"""An explicit PART1 line must not store the break hyphen twice.

An explicit hyphen pair carries its end-of-line hyphen structurally, in the
``<HYP>`` element — the last ``String``'s CONTENT is the bare fragment
(``préve``). When the LLM returns the fragment WITH a trailing hyphen
(``préve-``, natural since it sees a word-final break), the rewriter used to
store that hyphen in the String CONTENT *and* re-emit the ``<HYP>`` — the
physical line then carried the hyphen twice (``préve-`` + ``<HYP>``).

Observed 16× on BnF X0000002 through Mistral Small. A heuristic PART1 (no
HYP/SUBS markup) legitimately keeps its trailing dash in CONTENT — that case
is covered by ``test_rewriter``/byte-parity and must stay unchanged.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from corrigenda.formats.alto._ns import _detect_namespace
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.formats.alto.rewriter import rewrite_alto_file

_NS = "http://www.loc.gov/standards/alto/ns-v3#"

# Explicit PART1 (SUBS_TYPE + trailing <HYP>) split across two lines. The
# first String is deliberately garbled ("Novs") so a correction fires a
# rewrite path without changing the word count on L1 (fast path).
_ALTO = f"""<?xml version="1.0"?>
<alto xmlns="{_NS}">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="600" HEIGHT="800">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="600" HEIGHT="60">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="300" HEIGHT="30">
            <String ID="S1" CONTENT="Novs" HPOS="0" VPOS="0" WIDTH="90" HEIGHT="30"/>
            <SP ID="SP1" HPOS="90" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S2" CONTENT="le" HPOS="100" VPOS="0" WIDTH="40" HEIGHT="30"/>
            <SP ID="SP2" HPOS="140" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="S3" CONTENT="préve" SUBS_TYPE="HypPart1" SUBS_CONTENT="prévenons" HPOS="150" VPOS="0" WIDTH="140" HEIGHT="30"/>
            <HYP CONTENT="&#173;" HPOS="290" VPOS="0" WIDTH="10" HEIGHT="30"/>
          </TextLine>
          <TextLine ID="L2" HPOS="0" VPOS="30" WIDTH="300" HEIGHT="30">
            <String ID="S4" CONTENT="nons" SUBS_TYPE="HypPart2" SUBS_CONTENT="prévenons" HPOS="0" VPOS="30" WIDTH="120" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _rewrite(tmp_path: Path, corrected_l1: str):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    by_id = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    by_id["L1"].corrected_text = corrected_l1
    by_id["L2"].corrected_text = by_id["L2"].ocr_text
    xml_bytes, _m, paths = rewrite_alto_file(xml_path, doc.pages, "test", "mock")
    root = etree.fromstring(xml_bytes)
    ns = _detect_namespace(root)
    line = next(tl for tl in root.iter(f"{{{ns}}}TextLine") if tl.get("ID") == "L1")
    strings = [c for c in line if etree.QName(c.tag).localname == "String"]
    hyps = [c for c in line if etree.QName(c.tag).localname == "HYP"]
    return strings, hyps, paths


def test_fast_path_part1_no_double_hyphen(tmp_path: Path):
    # "Novs" -> "Nous": same word count on L1 → fast path. LLM returns the
    # fragment WITH a trailing hyphen.
    strings, hyps, paths = _rewrite(tmp_path, "Nous le préve-")
    assert paths["L1"] == "fast_path"
    # The break hyphen lives in the HYP element only, never doubled in CONTENT.
    assert strings[-1].get("CONTENT") == "préve"
    assert len(hyps) == 1
    # SUBS_CONTENT (the authoritative dehyphenated word) is preserved.
    assert strings[-1].get("SUBS_CONTENT") == "prévenons"


def test_slow_path_part1_no_double_hyphen(tmp_path: Path):
    # Add a word on L1 → word count changes → slow path (rebuild).
    strings, hyps, paths = _rewrite(tmp_path, "Or Nous le préve-")
    assert paths["L1"] == "slow_path"
    assert strings[-1].get("CONTENT") == "préve"
    assert len(hyps) == 1


def test_reconstructed_word_is_clean(tmp_path: Path):
    """End-to-end: the dehyphenated reading is 'prévenons', not 'préve-nons'."""
    strings, hyps, _ = _rewrite(tmp_path, "Nous le préve-")
    # Last fragment + PART2 fragment, joined on the structural hyphen.
    assert strings[-1].get("CONTENT") == "préve"
    assert hyps[0].get("CONTENT") in ("\xad", "-")
