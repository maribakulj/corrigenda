"""P0-5 — duplicate-identity refusal (audit: "mauvaise correction, mauvaise ligne").

Before this invariant existed, every correction-to-line association was a
bare ``line_id`` dict built with plain assignment: a file carrying two
``TextLine`` elements with the same ID silently applied the *last* parsed
manifest to *both* physical lines (rewriters), collapsed two lines into one
trace entry (``extract_output_texts``), and could surface the wrong trace.

The invariant now enforced end to end:

  * parsers refuse a file whose page/block/line IDs are not unique
    (``DuplicateIdError``, a ``ParseError``);
  * ``CorrectionPipeline.run()`` re-checks the manifest at the door, so
    hand-built manifests get the same guarantee;
  * both rewriters and both ``extract_output_texts`` fail loudly instead
    of overwriting (defence in depth for direct calls);
  * duplicate IDs across *different* source files stay legitimate — every
    downstream lookup is scoped to one file.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from corrigenda.core.pipeline import CorrectionPipeline
from corrigenda.core.schemas import (
    Coords,
    DocumentManifest,
    LineManifest,
    PageManifest,
)
from corrigenda.errors import (
    CorrectionError,
    DuplicateIdError,
    ParseError,
)
from corrigenda.formats.alto.parser import (
    build_document_manifest as build_alto_manifest,
)
from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.alto.rewriter import (
    extract_output_texts as extract_alto_texts,
)
from corrigenda.formats.alto.rewriter import rewrite_alto_file
from corrigenda.formats.page.parser import parse_page_file
from corrigenda.formats.page.rewriter import (
    extract_output_texts as extract_page_texts,
)
from corrigenda.formats.page.rewriter import rewrite_page_file

from tests._pipeline_harness import DictProvider, RecordingObserver, _NoopWriter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, xml: str, name: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(xml).strip(), encoding="utf-8")
    return p


def _alto(body: str, page_extra: str = "") -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="1000">
        {body}
      </PrintSpace>
    </Page>{page_extra}
  </Layout>
</alto>"""


def _alto_line(line_id: str, content: str, vpos: int = 10) -> str:
    return (
        f'<TextLine ID="{line_id}" HPOS="10" VPOS="{vpos}" WIDTH="500" HEIGHT="20">'
        f'<String CONTENT="{content}" HPOS="10" VPOS="{vpos}" WIDTH="500" HEIGHT="20"/>'
        "</TextLine>"
    )


ALTO_DUP_LINE = _alto(
    '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="100">'
    + _alto_line("TL1", "premiere", 10)
    + _alto_line("TL1", "seconde", 40)
    + "</TextBlock>"
)

ALTO_DUP_BLOCK = _alto(
    '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="100">'
    + _alto_line("TL1", "un", 10)
    + "</TextBlock>"
    + '<TextBlock ID="B1" HPOS="0" VPOS="200" WIDTH="1000" HEIGHT="100">'
    + _alto_line("TL2", "deux", 210)
    + "</TextBlock>"
)

ALTO_OK = _alto(
    '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="100">'
    + _alto_line("TL1", "un", 10)
    + _alto_line("TL2", "deux", 40)
    + "</TextBlock>"
)

# Two <Page> elements sharing ID="P1" in one file.
ALTO_DUP_PAGE = """\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="1000">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="100">
          <TextLine ID="TL1" HPOS="10" VPOS="10" WIDTH="500" HEIGHT="20">
            <String CONTENT="un" HPOS="10" VPOS="10" WIDTH="500" HEIGHT="20"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="1000">
        <TextBlock ID="B2" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="100">
          <TextLine ID="TL2" HPOS="10" VPOS="10" WIDTH="500" HEIGHT="20">
            <String CONTENT="deux" HPOS="10" VPOS="10" WIDTH="500" HEIGHT="20"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _page_xml(lines: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 100,0 100,50 0,50"/>
      {lines}
    </TextRegion>
  </Page>
</PcGts>"""


def _page_line(line_id: str, text: str) -> str:
    return (
        f'<TextLine id="{line_id}">'
        '<Coords points="0,0 100,0 100,20 0,20"/>'
        f"<TextEquiv><Unicode>{text}</Unicode></TextEquiv>"
        "</TextLine>"
    )


PAGE_DUP_LINE = _page_xml(_page_line("ln1", "premiere") + _page_line("ln1", "seconde"))


def _manifest_line(
    line_id: str,
    *,
    page_id: str = "P1",
    order: int = 0,
    corrected: str | None = None,
) -> LineManifest:
    lm = LineManifest(
        line_id=line_id,
        page_id=page_id,
        block_id="B1",
        line_order_global=order,
        line_order_in_block=order,
        coords=Coords(hpos=10, vpos=10 + 30 * order, width=500, height=20),
        ocr_text="texte",
    )
    if corrected is not None:
        lm.corrected_text = corrected
    return lm


def _page_manifest(
    lines: list[LineManifest],
    *,
    page_id: str = "P1",
    source_file: str = "doc.xml",
) -> PageManifest:
    return PageManifest(
        page_id=page_id,
        source_file=source_file,
        page_index=0,
        page_width=1000,
        page_height=1000,
        blocks=[],
        lines=lines,
    )


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_duplicate_id_error_is_parse_error_and_value_error():
    exc = DuplicateIdError("x")
    assert isinstance(exc, ParseError)
    assert isinstance(exc, CorrectionError)
    assert isinstance(exc, ValueError)


# ---------------------------------------------------------------------------
# Parsers refuse ambiguous files
# ---------------------------------------------------------------------------


def test_alto_parser_rejects_duplicate_line_id(tmp_path: Path):
    p = _write(tmp_path, ALTO_DUP_LINE, "dup.xml")
    with pytest.raises(DuplicateIdError) as ei:
        parse_alto_file(p, "dup.xml")
    assert "TL1" in str(ei.value)
    assert "dup.xml" in str(ei.value)


def test_alto_parser_rejects_duplicate_block_id(tmp_path: Path):
    p = _write(tmp_path, ALTO_DUP_BLOCK, "dupblock.xml")
    with pytest.raises(DuplicateIdError) as ei:
        parse_alto_file(p, "dupblock.xml")
    assert "B1" in str(ei.value)


def test_alto_parser_rejects_duplicate_page_id(tmp_path: Path):
    p = _write(tmp_path, ALTO_DUP_PAGE, "duppage.xml")
    with pytest.raises(DuplicateIdError) as ei:
        parse_alto_file(p, "duppage.xml")
    assert "P1" in str(ei.value)


def test_page_parser_rejects_duplicate_line_id(tmp_path: Path):
    p = _write(tmp_path, PAGE_DUP_LINE, "dup.xml")
    with pytest.raises(DuplicateIdError) as ei:
        parse_page_file(p, "dup.xml")
    assert "ln1" in str(ei.value)


def test_build_document_manifest_propagates_duplicate(tmp_path: Path):
    ok = _write(tmp_path, ALTO_OK, "ok.xml")
    dup = _write(tmp_path, ALTO_DUP_LINE, "dup.xml")
    with pytest.raises(DuplicateIdError):
        build_alto_manifest([(ok, "ok.xml"), (dup, "dup.xml")])


def test_same_line_ids_across_different_files_are_legitimate(tmp_path: Path):
    """Uniqueness is per file: two files may both use TL1/TL2."""
    a = _write(tmp_path, ALTO_OK, "a.xml")
    b = _write(tmp_path, ALTO_OK, "b.xml")
    doc = build_alto_manifest([(a, "a.xml"), (b, "b.xml")])
    assert doc.total_lines == 4  # nothing dropped, nothing collapsed


# ---------------------------------------------------------------------------
# Rewriters fail loudly instead of silently overwriting (defence in depth)
# ---------------------------------------------------------------------------


def test_alto_rewriter_rejects_duplicate_manifest_line_id(tmp_path: Path):
    p = _write(tmp_path, ALTO_OK, "ok.xml")
    page = _page_manifest(
        [
            _manifest_line("TL1", order=0, corrected="corrigé A"),
            _manifest_line("TL1", order=1, corrected="corrigé B"),
        ]
    )
    with pytest.raises(DuplicateIdError):
        rewrite_alto_file(p, [page], "prov", "model")


def test_alto_rewriter_rejects_duplicate_element_id(tmp_path: Path):
    """Historical corruption: both TL1 elements received the same manifest's
    correction (last-write-wins on the lookup). Now refused explicitly."""
    p = _write(tmp_path, ALTO_DUP_LINE, "dup.xml")
    page = _page_manifest([_manifest_line("TL1", corrected="corrigé")])
    with pytest.raises(DuplicateIdError):
        rewrite_alto_file(p, [page], "prov", "model")


def test_page_rewriter_rejects_duplicate_manifest_line_id(tmp_path: Path):
    p = _write(tmp_path, _page_xml(_page_line("ln1", "texte")), "ok.xml")
    page = _page_manifest(
        [
            _manifest_line("ln1", order=0, corrected="corrigé A"),
            _manifest_line("ln1", order=1, corrected="corrigé B"),
        ]
    )
    with pytest.raises(DuplicateIdError):
        rewrite_page_file(p, [page], "prov", "model")


def test_page_rewriter_rejects_duplicate_element_id(tmp_path: Path):
    p = _write(tmp_path, PAGE_DUP_LINE, "dup.xml")
    page = _page_manifest([_manifest_line("ln1", corrected="corrigé")])
    with pytest.raises(DuplicateIdError):
        rewrite_page_file(p, [page], "prov", "model")


def test_alto_extract_output_texts_rejects_duplicate_element_id():
    xml = ALTO_DUP_LINE.encode("utf-8")
    with pytest.raises(DuplicateIdError):
        extract_alto_texts(xml, {"TL1"})


def test_page_extract_output_texts_rejects_duplicate_element_id():
    xml = PAGE_DUP_LINE.encode("utf-8")
    with pytest.raises(DuplicateIdError):
        extract_page_texts(xml, {"ln1"})


# ---------------------------------------------------------------------------
# Pipeline validates the manifest at the door (hand-built manifests too)
# ---------------------------------------------------------------------------


def _pipeline() -> CorrectionPipeline:
    return CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
    )


def test_pipeline_run_rejects_duplicate_line_id_in_manifest(tmp_path: Path):
    page = _page_manifest(
        [_manifest_line("TL1", order=0), _manifest_line("TL1", order=1)]
    )
    doc = DocumentManifest(
        source_files=["doc.xml"],
        pages=[page],
        total_pages=1,
        total_blocks=0,
        total_lines=2,
    )
    with pytest.raises(DuplicateIdError):
        _pipeline().run_sync(
            document_manifest=doc,
            source_files={"doc.xml": tmp_path / "doc.xml"},
            apply=False,
        )


def test_pipeline_run_rejects_cross_file_page_id_collision(tmp_path: Path):
    page_a = _page_manifest([_manifest_line("TL1")], source_file="a.xml")
    page_b = _page_manifest([_manifest_line("TL9")], source_file="b.xml")
    # Same page_id "P1" in two different files: trace keys and per-page
    # lookups would collide. build_document_manifest disambiguates this;
    # a hand-built manifest must be refused.
    doc = DocumentManifest(
        source_files=["a.xml", "b.xml"],
        pages=[page_a, page_b],
        total_pages=2,
        total_blocks=0,
        total_lines=2,
    )
    with pytest.raises(DuplicateIdError):
        _pipeline().run_sync(
            document_manifest=doc,
            source_files={"a.xml": tmp_path / "a.xml", "b.xml": tmp_path / "b.xml"},
            apply=False,
        )


def test_pipeline_end_to_end_with_cross_file_reused_line_ids(tmp_path: Path):
    """Legitimate reuse across files runs end to end (dry-run) untouched."""
    a = _write(tmp_path, ALTO_OK, "a.xml")
    b = _write(tmp_path, ALTO_OK, "b.xml")
    doc = build_alto_manifest([(a, "a.xml"), (b, "b.xml")])
    result = _pipeline().run_sync(
        document_manifest=doc,
        source_files={"a.xml": a, "b.xml": b},
        apply=False,
    )
    assert result.report is not None
    assert doc.total_lines == 4
