"""Tests for alto/parser.py"""

from __future__ import annotations

import textwrap
from pathlib import Path

from app.alto.parser import build_document_manifest, parse_alto_file
from app.schemas import HyphenRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_alto(tmp_path: Path, xml: str, name: str = "test.xml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(xml).strip(), encoding="utf-8")
    return p


def alto_v3(body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="2480" HEIGHT="3508">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">
        {body}
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def alto_v2(body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://schema.ccs-gmbh.com/ALTO">
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
        {body}
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def alto_v4(body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
        {body}
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def alto_nons(body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto>
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
        {body}
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


SIMPLE_BLOCK = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20">
    <String ID="S1" CONTENT="Hello" HPOS="0" VPOS="0" WIDTH="50" HEIGHT="20"/>
    <SP WIDTH="5"/>
    <String ID="S2" CONTENT="world" HPOS="55" VPOS="0" WIDTH="45" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""


# ---------------------------------------------------------------------------
# test_namespace_v2_v3_v4_none
# ---------------------------------------------------------------------------


def test_namespace_v2_v3_v4_none(tmp_path):
    for template in (alto_v2, alto_v3, alto_v4, alto_nons):
        xml_path = write_alto(tmp_path, template(SIMPLE_BLOCK))
        pages, _ = parse_alto_file(xml_path, "test.xml")
        assert len(pages) == 1
        assert len(pages[0].lines) == 1
        assert pages[0].lines[0].ocr_text == "Hello world"


# ---------------------------------------------------------------------------
# test_ocr_text_string_sp_hyp
# ---------------------------------------------------------------------------


def test_ocr_text_string_sp_hyp(tmp_path):
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="por" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="20"
            SUBS_TYPE="HypPart1" SUBS_CONTENT="porte"/>
    <HYP CONTENT="-"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    line = pages[0].lines[0]
    # ocr_text includes the HYP content
    assert "por" in line.ocr_text
    assert "-" in line.ocr_text


def test_ocr_text_sp_produces_space(tmp_path):
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="foo" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="20"/>
    <SP WIDTH="10"/>
    <String ID="S2" CONTENT="bar" HPOS="70" VPOS="0" WIDTH="60" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    assert pages[0].lines[0].ocr_text == "foo bar"


def test_ocr_text_hyp_no_content(tmp_path):
    """HYP without CONTENT attribute defaults to '-'."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="fin" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="20"/>
    <HYP/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    assert pages[0].lines[0].ocr_text == "fin-"


# ---------------------------------------------------------------------------
# test_page_manifest_counts
# ---------------------------------------------------------------------------


def test_page_manifest_counts(tmp_path):
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="200">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="Line1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="Line2" HPOS="0" VPOS="25" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>
<TextBlock ID="TB2" HPOS="0" VPOS="100" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL3" HPOS="0" VPOS="100" WIDTH="200" HEIGHT="20">
    <String ID="S3" CONTENT="Line3" HPOS="0" VPOS="100" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    assert len(pages) == 1
    page = pages[0]
    assert len(page.blocks) == 2
    assert len(page.lines) == 3
    assert page.blocks[0].block_id == "TB1"
    assert page.blocks[1].block_id == "TB2"
    assert len(page.blocks[0].line_ids) == 2
    assert len(page.blocks[1].line_ids) == 1


# ---------------------------------------------------------------------------
# test_prev_next_links
# ---------------------------------------------------------------------------


def test_prev_next_links(tmp_path):
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="200">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="A" HPOS="0" VPOS="0" WIDTH="20" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="B" HPOS="0" VPOS="25" WIDTH="20" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL3" HPOS="0" VPOS="50" WIDTH="200" HEIGHT="20">
    <String ID="S3" CONTENT="C" HPOS="0" VPOS="50" Width="20" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].prev_line_id is None
    assert lines[0].next_line_id == "TL2"
    assert lines[1].prev_line_id == "TL1"
    assert lines[1].next_line_id == "TL3"
    assert lines[2].prev_line_id == "TL2"
    assert lines[2].next_line_id is None


# ---------------------------------------------------------------------------
# test_hyphen_explicit_subs_type
# ---------------------------------------------------------------------------


def test_hyphen_explicit_subs_type(tmp_path):
    """SUBS_TYPE=HypPart1 on last String → PART1, explicit."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="Il" HPOS="0" VPOS="0" WIDTH="30" HEIGHT="20"/>
    <SP WIDTH="5"/>
    <String ID="S2" CONTENT="por-" HPOS="35" VPOS="0" WIDTH="60" HEIGHT="20"
            SUBS_TYPE="HypPart1" SUBS_CONTENT="porte"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S3" CONTENT="te" HPOS="0" VPOS="25" WIDTH="40" HEIGHT="20"
            SUBS_TYPE="HypPart2" SUBS_CONTENT="porte"/>
    <SP WIDTH="5"/>
    <String ID="S4" CONTENT="ouverte" HPOS="45" VPOS="25" WIDTH="80" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].hyphen_role == HyphenRole.PART1
    assert lines[0].hyphen_source_explicit is True
    assert lines[0].hyphen_subs_content == "porte"
    assert lines[1].hyphen_role == HyphenRole.PART2
    assert lines[1].hyphen_source_explicit is True
    assert lines[1].hyphen_subs_content == "porte"


# ---------------------------------------------------------------------------
# test_hyphen_explicit_hyp_element
# ---------------------------------------------------------------------------


def test_hyphen_explicit_hyp_element(tmp_path):
    """HYP element at end of line → PART1, source_explicit=True."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="con" HPOS="0" VPOS="0" WIDTH="50" HEIGHT="20"
            SUBS_TYPE="HypPart1" SUBS_CONTENT="construction"/>
    <HYP CONTENT="-"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="struction" HPOS="0" VPOS="25" WIDTH="100" HEIGHT="20"
            SUBS_TYPE="HypPart2" SUBS_CONTENT="construction"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].hyphen_role == HyphenRole.PART1
    assert lines[0].hyphen_source_explicit is True
    assert lines[1].hyphen_role == HyphenRole.PART2


# ---------------------------------------------------------------------------
# test_hyphen_heuristic
# ---------------------------------------------------------------------------


def test_hyphen_heuristic(tmp_path):
    """Last token ending with '-' and no SUBS_TYPE → heuristic PART1."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="boule-" HPOS="0" VPOS="0" WIDTH="80" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="vard" HPOS="0" VPOS="25" Width="60" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].hyphen_role == HyphenRole.PART1
    assert lines[0].hyphen_source_explicit is False
    assert lines[0].hyphen_subs_content is None


# ---------------------------------------------------------------------------
# test_hyphen_pair_bidirectional
# ---------------------------------------------------------------------------


def test_hyphen_pair_bidirectional(tmp_path):
    """PART1 and PART2 must point to each other via hyphen_pair_line_id."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="tra-" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="20"
            SUBS_TYPE="HypPart1" SUBS_CONTENT="travail"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="vail" HPOS="0" VPOS="25" Width="60" HEIGHT="20"
            SUBS_TYPE="HypPart2" SUBS_CONTENT="travail"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].hyphen_pair_line_id == lines[1].line_id
    assert lines[1].hyphen_pair_line_id == lines[0].line_id


# ---------------------------------------------------------------------------
# test_hyphen_subs_content_propagated
# ---------------------------------------------------------------------------


def test_hyphen_subs_content_propagated(tmp_path):
    """SUBS_CONTENT on PART1 only → propagated to PART2 and vice-versa."""
    # SUBS_CONTENT on PART1 only
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="fon-" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="20"
            SUBS_TYPE="HypPart1" SUBS_CONTENT="fondation"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="dation" HPOS="0" VPOS="25" Width="80" HEIGHT="20"
            SUBS_TYPE="HypPart2"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].hyphen_subs_content == "fondation"
    assert lines[1].hyphen_subs_content == "fondation"


def test_hyphen_subs_content_propagated_from_part2(tmp_path):
    """SUBS_CONTENT on PART2 only → propagated to PART1."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="fon-" HPOS="0" VPOS="0" WIDTH="60" HEIGHT="20"
            SUBS_TYPE="HypPart1"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="dation" HPOS="0" VPOS="25" Width="80" HEIGHT="20"
            SUBS_TYPE="HypPart2" SUBS_CONTENT="fondation"/>
  </TextLine>
</TextBlock>"""
    xml_path = write_alto(tmp_path, alto_v3(body))
    pages, _ = parse_alto_file(xml_path, "test.xml")
    lines = pages[0].lines
    assert lines[0].hyphen_subs_content == "fondation"
    assert lines[1].hyphen_subs_content == "fondation"


# ---------------------------------------------------------------------------
# test_multi_file
# ---------------------------------------------------------------------------


def test_multi_file(tmp_path):
    """Two XML files → continuous global line indices, all pages present."""
    file1 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="File1Line1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="File1Line2" HPOS="0" VPOS="25" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "file1.xml",
    )
    file2 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="File2Line1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "file2.xml",
    )

    doc = build_document_manifest([(file1, "file1.xml"), (file2, "file2.xml")])
    assert doc.total_pages == 2
    assert doc.total_lines == 3
    assert doc.total_blocks == 2
    assert doc.source_files == ["file1.xml", "file2.xml"]

    all_lines = [l for p in doc.pages for l in p.lines]
    global_orders = [l.line_order_global for l in all_lines]
    assert global_orders == [0, 1, 2]


# ---------------------------------------------------------------------------
# Cross-page hyphenation linking
# ---------------------------------------------------------------------------


def test_cross_page_hyphen_pair_linked(tmp_path: Path):
    """PART1 on last line of page 1 should link to PART2 on first line of page 2."""
    file1 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="les" HPOS="0" VPOS="0" WIDTH="40" HEIGHT="20"/>
    <SP HPOS="40" VPOS="0" WIDTH="10"/>
    <String ID="S2" CONTENT="fonda" SUBS_TYPE="HypPart1" SUBS_CONTENT="fondamentaux" HPOS="50" VPOS="0" WIDTH="60" HEIGHT="20"/>
    <HYP CONTENT="-" HPOS="110" VPOS="0" WIDTH="10"/>
  </TextLine>
</TextBlock>"""),
        "page1.xml",
    )
    file2 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="mentaux" SUBS_TYPE="HypPart2" SUBS_CONTENT="fondamentaux" HPOS="0" VPOS="0" WIDTH="80" HEIGHT="20"/>
    <SP HPOS="80" VPOS="0" WIDTH="10"/>
    <String ID="S2" CONTENT="du" HPOS="90" VPOS="0" WIDTH="30" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "page2.xml",
    )

    doc = build_document_manifest([(file1, "page1.xml"), (file2, "page2.xml")])

    page1_last = doc.pages[0].lines[-1]
    page2_first = doc.pages[1].lines[0]

    # PART1 on page 1 should be linked to PART2 on page 2
    assert page1_last.hyphen_role == HyphenRole.PART1
    assert page1_last.hyphen_pair_line_id == page2_first.line_id

    # PART2 on page 2 should be linked back to PART1 on page 1
    assert page2_first.hyphen_role == HyphenRole.PART2
    assert page2_first.hyphen_pair_line_id == page1_last.line_id

    # Both should share the subs_content
    assert page1_last.hyphen_subs_content == "fondamentaux"
    assert page2_first.hyphen_subs_content == "fondamentaux"


def test_cross_page_heuristic_hyphen_linked(tmp_path: Path):
    """Heuristic PART1 (trailing dash, no SUBS_TYPE) also links cross-page."""
    file1 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="pratica-" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "page1.xml",
    )
    file2 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="bles" HPOS="0" VPOS="0" WIDTH="50" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "page2.xml",
    )

    doc = build_document_manifest([(file1, "page1.xml"), (file2, "page2.xml")])

    page1_last = doc.pages[0].lines[-1]
    page2_first = doc.pages[1].lines[0]

    assert page1_last.hyphen_role == HyphenRole.PART1
    assert page1_last.hyphen_pair_line_id == page2_first.line_id
    assert page2_first.hyphen_role == HyphenRole.PART2
    assert page2_first.hyphen_pair_line_id == page1_last.line_id


def test_single_page_no_cross_page_link(tmp_path: Path):
    """A single page with orphan PART1 should NOT be linked (no partner)."""
    file1 = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="fonda-" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "page1.xml",
    )

    doc = build_document_manifest([(file1, "page1.xml")])

    orphan = doc.pages[0].lines[-1]
    assert orphan.hyphen_role == HyphenRole.PART1
    assert orphan.hyphen_pair_line_id is None  # no partner available


# ---------------------------------------------------------------------------
# B-007 — _detect_namespace defensive against malformed tags
# ---------------------------------------------------------------------------


def test_detect_namespace_handles_missing_closing_brace():
    """A tag that starts with '{' but lacks '}' must not crash with ValueError."""
    # Private helper — pulled from alto-core directly since the
    # backend re-export shim no longer surfaces privates.
    from alto_core.alto.parser import _detect_namespace

    class FakeElement:
        tag = "{noclosingbrace"

    # Before the fix this raised ValueError. Should return '' now.
    assert _detect_namespace(FakeElement()) == ""


def test_detect_namespace_plain_tag():
    # Private helper — pulled from alto-core directly since the
    # backend re-export shim no longer surfaces privates.
    from alto_core.alto.parser import _detect_namespace

    class FakeElement:
        tag = "alto"

    assert _detect_namespace(FakeElement()) == ""


def test_detect_namespace_normal_namespaced_tag():
    # Private helper — pulled from alto-core directly since the
    # backend re-export shim no longer surfaces privates.
    from alto_core.alto.parser import _detect_namespace

    class FakeElement:
        tag = "{http://www.loc.gov/standards/alto/ns-v3#}alto"

    assert _detect_namespace(FakeElement()) == "http://www.loc.gov/standards/alto/ns-v3#"


# ---------------------------------------------------------------------------
# B-006 — disambiguate_page_ids on cross-file Page ID collision
# ---------------------------------------------------------------------------


def test_colliding_page_ids_disambiguated_across_files(tmp_path: Path):
    """Two files declaring Page ID='P1' must get distinct page_ids in the manifest."""
    body = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="hello" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    file1 = write_alto(tmp_path, alto_v3(body), "fileA.xml")
    file2 = write_alto(tmp_path, alto_v3(body), "fileB.xml")

    doc = build_document_manifest([(file1, "fileA.xml"), (file2, "fileB.xml")])

    pids = [p.page_id for p in doc.pages]
    assert len(set(pids)) == 2, f"Page IDs collide: {pids}"
    # Format is "{source}::{original_pid}"
    assert any("fileA.xml" in pid for pid in pids)
    assert any("fileB.xml" in pid for pid in pids)


def test_unique_page_ids_left_intact(tmp_path: Path):
    """If page IDs don't collide, no renaming happens (backward compat)."""
    body_a = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="a" HPOS="0" VPOS="0" WIDTH="50" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    file1 = write_alto(tmp_path, alto_v3(body_a), "fileA.xml")
    # second file with a different Page ID
    body_b = body_a  # but we'll edit the wrapper
    file2_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">'
        '<Layout><Page ID="P2" WIDTH="2480" HEIGHT="3508">'
        '<PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">'
        + body_b
        + "</PrintSpace></Page></Layout></alto>"
    )
    file2 = tmp_path / "fileB.xml"
    file2.write_text(file2_xml, encoding="utf-8")

    doc = build_document_manifest([(file1, "fileA.xml"), (file2, "fileB.xml")])
    pids = [p.page_id for p in doc.pages]
    assert pids == ["P1", "P2"], f"Expected clean page_ids, got {pids}"


def test_colliding_pages_with_cross_page_hyphen(tmp_path: Path):
    """B-005 / B-006 regression: cross-page hyphen with colliding Page+Line IDs.

    Two files both declare Page ID='P1' and lines named 'TL1'/'TL2'.
    File A's last line is PART1 (trailing dash) → linked to file B's first line.
    After disambiguation, hyphen_pair_page_id on each side points to the
    OTHER file's qualified page_id, so the orchestrator's qualified
    lookup resolves the correct partner.
    """
    file_a = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="middle" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="fonda-" HPOS="0" VPOS="25" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "fileA.xml",
    )
    file_b = write_alto(
        tmp_path,
        alto_v3("""\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="mentaux" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""),
        "fileB.xml",
    )

    doc = build_document_manifest([(file_a, "fileA.xml"), (file_b, "fileB.xml")])

    page_a, page_b = doc.pages[0], doc.pages[1]
    # Page IDs were renamed because of collision
    assert page_a.page_id != page_b.page_id

    last_a = page_a.lines[-1]
    first_b = page_b.lines[0]

    assert last_a.hyphen_role == HyphenRole.PART1
    assert last_a.hyphen_pair_line_id == first_b.line_id
    # Critically: hyphen_pair_page_id points to file B's qualified page_id,
    # not the ambiguous original 'P1'.
    assert last_a.hyphen_pair_page_id == page_b.page_id
    assert first_b.hyphen_pair_page_id == page_a.page_id
