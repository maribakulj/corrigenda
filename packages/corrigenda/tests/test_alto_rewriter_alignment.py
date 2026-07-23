"""ROADMAP V3 Phase 1 — slow-path identity recycling follows the ALIGNMENT.

Positional recycling attached text to the wrong word identity: an
inserted word shifted every following token onto the previous token's
ID/STYLE (the review's 'réordonnancement de mots → texte associé à la
mauvaise géométrie' family). Identity now follows the token each word
actually corresponds to; unmatched styled sources are counted losses;
suspected reorders are flagged (`word_order_suspected`), never acted on.
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
            <String ID="W1" CONTENT="aaa" HPOS="0" VPOS="0" WIDTH="90" HEIGHT="30"/>
            <SP HPOS="90" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="W2" CONTENT="bbb" HPOS="100" VPOS="0" WIDTH="90" HEIGHT="30"/>
            <SP HPOS="190" VPOS="0" WIDTH="10" HEIGHT="30"/>
            <String ID="W3" CONTENT="ccc" STYLE="bold" HPOS="200" VPOS="0" WIDTH="100" HEIGHT="30"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _rewrite(tmp_path: Path, corrected: str):
    xml_path = tmp_path / "p.xml"
    xml_path.write_text(_ALTO, encoding="utf-8")
    doc = build_document_manifest([(xml_path, xml_path.name)])
    by_id = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    by_id["L1"].corrected_text = corrected
    return rewrite_alto_file(xml_path, doc.pages, "test", "mock")


def _strings(result) -> list[etree._Element]:
    root = etree.fromstring(result.xml_bytes)
    ns = _detect_namespace(root)
    return list(root.iter(f"{{{ns}}}String"))


def test_insertion_keeps_identity_on_the_right_words(tmp_path: Path):
    """Failed under positional recycling: 'xyz' took W2's identity and
    W3's bold STYLE landed on 'bbb'."""
    result = _rewrite(tmp_path, "aaa xyz bbb ccc")
    assert result.rewriter_paths["L1"] == "slow_path"

    by_content = {s.get("CONTENT"): s for s in _strings(result)}
    assert by_content["aaa"].get("ID") == "W1"
    assert by_content["bbb"].get("ID") == "W2"
    assert by_content["ccc"].get("ID") == "W3"
    assert by_content["ccc"].get("STYLE") == "bold"
    # The inserted token gets a fresh generated ID, no stolen identity.
    inserted_id = by_content["xyz"].get("ID")
    assert inserted_id not in ("W1", "W2", "W3")
    assert by_content["xyz"].get("STYLE") is None
    # Nothing was lost: every styled source found its word.
    assert "style_dropped" not in result.losses
    assert "word_order_suspected" not in result.losses


def test_deletion_keeps_identity_on_the_survivors(tmp_path: Path):
    result = _rewrite(tmp_path, "aaa ccc")
    by_content = {s.get("CONTENT"): s for s in _strings(result)}
    assert by_content["aaa"].get("ID") == "W1"
    assert by_content["ccc"].get("ID") == "W3"
    assert by_content["ccc"].get("STYLE") == "bold"


def test_unmatched_styled_source_is_a_counted_loss(tmp_path: Path):
    """'ccc' (bold) is fully replaced by a zero-similarity token AND the
    count changes: no correspondence, so the STYLE is dropped — and
    COUNTED, never silently."""
    result = _rewrite(tmp_path, "aaa bbb zzz www")
    assert result.rewriter_paths["L1"] == "slow_path"
    by_content = {s.get("CONTENT"): s for s in _strings(result)}
    assert by_content["zzz"].get("STYLE") is None
    assert result.losses.get("style_dropped") == 1
    assert result.losses_by_line["L1"].get("style_dropped") == 1


def test_suspected_word_reorder_is_flagged_not_applied(tmp_path: Path):
    result = _rewrite(tmp_path, "bbb aaa ccc extra")
    assert result.losses_by_line["L1"].get("word_order_suspected") == 1
    # Flagged, never acted on: the text is written exactly as corrected.
    contents = [s.get("CONTENT") for s in _strings(result)]
    assert contents == ["bbb", "aaa", "ccc", "extra"]


def test_generated_ids_never_collide_with_recycled_ones(tmp_path: Path):
    """A generated ID for an inserted token must dodge both recycled
    source IDs and previously generated ones."""
    result = _rewrite(tmp_path, "aaa xyz qqq bbb ccc")
    ids = [s.get("ID") for s in _strings(result)]
    assert len(ids) == len(set(ids)), f"duplicate String IDs: {ids}"
