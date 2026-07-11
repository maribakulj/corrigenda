"""P1-1 — recursive structure traversal + explicit reading order.

Historically both parsers only visited *direct* children (``findall``):

  * ALTO ``TextBlock``s nested inside a ``ComposedBlock`` (articles,
    figure groups) were silently dropped — their lines never entered the
    manifest, were never corrected, and never counted;
  * PAGE ``TextRegion``s nested inside another region: same silent drop;
  * declared reading order (ALTO ``IDNEXT`` chains, PAGE ``ReadingOrder``)
    was ignored — prev/next neighbours and hyphen pairing followed raw
    XML order, wrong on multicolumn layouts whose declaration diverges.

These tests pin the fixed behaviour, including the conservative
fallbacks (inconsistent declarations degrade to document order — the
library never guesses).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.page.parser import parse_page_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, xml: str, name: str = "t.xml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(xml).strip(), encoding="utf-8")
    return p


def _alto_doc(printspace_body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="2000" HEIGHT="3000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="2000" HEIGHT="3000">
        {printspace_body}
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _tb(block_id: str, content: str, idnext: str | None = None, vpos: int = 10) -> str:
    nxt = f' IDNEXT="{idnext}"' if idnext else ""
    return (
        f'<TextBlock ID="{block_id}"{nxt} HPOS="10" VPOS="{vpos}" '
        'WIDTH="900" HEIGHT="40">'
        f'<TextLine ID="TL_{block_id}" HPOS="10" VPOS="{vpos}" WIDTH="900" HEIGHT="20">'
        f'<String CONTENT="{content}" HPOS="10" VPOS="{vpos}" WIDTH="900" HEIGHT="20"/>'
        "</TextLine></TextBlock>"
    )


def _line_ids(pages) -> list[str]:
    return [lm.line_id for p in pages for lm in p.lines]


def _texts(pages) -> list[str]:
    return [lm.ocr_text for p in pages for lm in p.lines]


# ---------------------------------------------------------------------------
# ALTO — ComposedBlock descent
# ---------------------------------------------------------------------------


def test_alto_composed_block_lines_are_parsed(tmp_path: Path):
    """Lines inside a ComposedBlock used to be silently dropped."""
    xml = _alto_doc(
        _tb("B1", "avant", vpos=10)
        + '<ComposedBlock ID="CB1" HPOS="10" VPOS="100" WIDTH="900" HEIGHT="200">'
        + _tb("B2", "dedans", vpos=110)
        + "</ComposedBlock>"
        + _tb("B3", "apres", vpos=400)
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["avant", "dedans", "apres"]
    assert [b.block_id for b in pages[0].blocks] == ["B1", "B2", "B3"]


def test_alto_deeply_nested_composed_blocks(tmp_path: Path):
    xml = _alto_doc(
        '<ComposedBlock ID="CB1" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="900">'
        '<ComposedBlock ID="CB2" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="400">'
        + _tb("B1", "profond", vpos=10)
        + "</ComposedBlock>"
        + _tb("B2", "moins", vpos=500)
        + "</ComposedBlock>"
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["profond", "moins"]


def test_alto_neighbour_links_span_composed_boundaries(tmp_path: Path):
    xml = _alto_doc(
        _tb("B1", "un", vpos=10)
        + '<ComposedBlock ID="CB1" HPOS="0" VPOS="100" WIDTH="900" HEIGHT="200">'
        + _tb("B2", "deux", vpos=110)
        + "</ComposedBlock>"
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    lines = pages[0].lines
    assert lines[0].next_line_id == "TL_B2"
    assert lines[1].prev_line_id == "TL_B1"


# ---------------------------------------------------------------------------
# ALTO — IDNEXT reading-order chains
# ---------------------------------------------------------------------------


def test_alto_idnext_chain_overrides_document_order(tmp_path: Path):
    # Document order B1, B2, B3 — declared reading order B1 → B3 → B2.
    xml = _alto_doc(
        _tb("B1", "premier", idnext="B3", vpos=10)
        + _tb("B2", "troisieme", vpos=50)
        + _tb("B3", "deuxieme", idnext="B2", vpos=90)
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["premier", "deuxieme", "troisieme"]
    # Neighbour links follow the declared order, not XML order.
    lines = pages[0].lines
    assert [lm.line_id for lm in lines] == ["TL_B1", "TL_B3", "TL_B2"]
    assert lines[0].next_line_id == "TL_B3"
    assert lines[1].prev_line_id == "TL_B1"
    # block_order reflects reading order.
    assert [b.block_id for b in pages[0].blocks] == ["B1", "B3", "B2"]


def test_alto_idnext_dangling_ref_falls_back_to_document_order(tmp_path: Path):
    xml = _alto_doc(
        _tb("B1", "un", idnext="NOPE", vpos=10) + _tb("B2", "deux", vpos=50)
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["un", "deux"]


def test_alto_idnext_cycle_falls_back_to_document_order(tmp_path: Path):
    xml = _alto_doc(
        _tb("B1", "un", idnext="B2", vpos=10)
        + _tb("B2", "deux", idnext="B1", vpos=50)
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["un", "deux"]


def test_alto_idnext_converging_chains_fall_back(tmp_path: Path):
    # Two blocks both declare B3 as successor — ambiguous.
    xml = _alto_doc(
        _tb("B1", "un", idnext="B3", vpos=10)
        + _tb("B2", "deux", idnext="B3", vpos=50)
        + _tb("B3", "trois", vpos=90)
    )
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["un", "deux", "trois"]


# ---------------------------------------------------------------------------
# PAGE — nested regions + ReadingOrder
# ---------------------------------------------------------------------------


def _page_doc(body: str, reading_order: str = "") -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    {reading_order}
    {body}
  </Page>
</PcGts>"""


def _tr(region_id: str, text: str, inner: str = "") -> str:
    return (
        f'<TextRegion id="{region_id}">'
        '<Coords points="0,0 100,0 100,50 0,50"/>'
        f'<TextLine id="ln_{region_id}">'
        '<Coords points="0,0 100,0 100,20 0,20"/>'
        f"<TextEquiv><Unicode>{text}</Unicode></TextEquiv>"
        "</TextLine>"
        f"{inner}"
        "</TextRegion>"
    )


def _ro(*refs: str) -> str:
    items = "".join(
        f'<RegionRefIndexed index="{i}" regionRef="{r}"/>' for i, r in enumerate(refs)
    )
    return f'<ReadingOrder><OrderedGroup id="g0">{items}</OrderedGroup></ReadingOrder>'


def test_page_nested_region_lines_are_parsed(tmp_path: Path):
    """Lines of a region nested inside another region used to be dropped."""
    xml = _page_doc(_tr("r1", "parent", inner=_tr("r1a", "enfant")))
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["parent", "enfant"]
    # Each line is attributed to its own region's block.
    assert [lm.block_id for lm in pages[0].lines] == ["r1", "r1a"]


def test_page_reading_order_overrides_document_order(tmp_path: Path):
    # Document order r2, r1 — declared reading order r1 then r2
    # (the multicolumn case: XML serialised column 2 first).
    xml = _page_doc(
        _tr("r2", "colonne deux") + _tr("r1", "colonne une"),
        reading_order=_ro("r1", "r2"),
    )
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["colonne une", "colonne deux"]
    lines = pages[0].lines
    assert lines[0].next_line_id == "ln_r2"
    assert lines[1].prev_line_id == "ln_r1"


def test_page_reading_order_respects_index_not_document_position(tmp_path: Path):
    # RegionRefIndexed serialised out of index order.
    ro = (
        "<ReadingOrder><OrderedGroup id=\"g0\">"
        '<RegionRefIndexed index="1" regionRef="r1"/>'
        '<RegionRefIndexed index="0" regionRef="r2"/>'
        "</OrderedGroup></ReadingOrder>"
    )
    xml = _page_doc(_tr("r1", "second") + _tr("r2", "premier"), reading_order=ro)
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["premier", "second"]


def test_page_reading_order_nested_groups(tmp_path: Path):
    ro = (
        "<ReadingOrder><OrderedGroup id=\"g0\">"
        '<RegionRefIndexed index="0" regionRef="r3"/>'
        '<OrderedGroupIndexed index="1" id="g1">'
        '<RegionRefIndexed index="0" regionRef="r2"/>'
        '<RegionRefIndexed index="1" regionRef="r1"/>'
        "</OrderedGroupIndexed>"
        "</OrderedGroup></ReadingOrder>"
    )
    xml = _page_doc(
        _tr("r1", "c") + _tr("r2", "b") + _tr("r3", "a"), reading_order=ro
    )
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["a", "b", "c"]


def test_page_unreferenced_regions_keep_document_order_after_declared(
    tmp_path: Path,
):
    xml = _page_doc(
        _tr("r9", "hors declaration") + _tr("r2", "deux") + _tr("r1", "une"),
        reading_order=_ro("r1", "r2"),
    )
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["une", "deux", "hors declaration"]


def test_page_reading_order_with_only_dangling_refs_is_harmless(tmp_path: Path):
    xml = _page_doc(
        _tr("r1", "un") + _tr("r2", "deux"),
        reading_order=_ro("ghost1", "ghost2"),
    )
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    assert _texts(pages) == ["un", "deux"]


def test_page_multicolumn_hyphen_pairs_follow_reading_order(tmp_path: Path):
    """The P1-2 prerequisite: with a declared reading order, a PART1 line at
    the bottom of column 1 now sees column 1's real successor — not
    whatever region happened to come next in raw XML order."""
    # Realistic multicolumn geometry: the hyphenated line sits at the
    # BOTTOM of column 1; its continuation is the TOP line of column 2.
    col2 = (
        '<TextRegion id="col2"><Coords points="510,0 1000,0 1000,900 510,900"/>'
        '<TextLine id="c2l1"><Coords points="510,10 1000,10 1000,30 510,30"/>'
        "<TextEquiv><Unicode>suite du mot</Unicode></TextEquiv></TextLine>"
        "</TextRegion>"
    )
    col1 = (
        '<TextRegion id="col1"><Coords points="0,0 500,0 500,900 0,900"/>'
        '<TextLine id="c1l1"><Coords points="0,870 500,870 500,890 0,890"/>'
        "<TextEquiv><Unicode>debut coupe-</Unicode></TextEquiv></TextLine>"
        "</TextRegion>"
    )
    # XML serialises col2 first; the declaration says col1 reads first.
    xml = _page_doc(col2 + col1, reading_order=_ro("col1", "col2"))
    pages, _ = parse_page_file(_write(tmp_path, xml), "t.xml")
    lines = {lm.line_id: lm for lm in pages[0].lines}
    assert lines["c1l1"].hyphen_pair_line_id == "c2l1"
