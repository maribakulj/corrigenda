"""Adversarial-review fixes over the audit remediation wave.

Each test pins one confirmed review finding:

  * planner: pydantic ``model_copy(update=…)`` BYPASSES validation, so the
    window walk needs its own progress guard (reproduced infinite loop);
  * planner: the LINE-mode chain cap must UNLINK the cut pair — a
    still-linked pair split across chunks violates pair atomicity;
  * ALTO parser: empty-string block IDs crashed the IDNEXT walk with a
    raw KeyError; an IDNEXT pointing outside the page (cross-page article
    continuation — a legitimate METS/ALTO pattern) must end the chain,
    not void the whole declared order;
  * ALTO parser: without ``PrintSpace``, margin-nested blocks must stay
    out of correction scope (the recursive walk swept them in);
  * parsers: the duplicate-ID gate must scan the WHOLE tree (the
    rewriters match document-wide, so a margin line reusing a body ID
    used to explode only at rewrite time, after the full LLM spend);
  * identity: block IDs are page-scoped — per-page OCR exports reusing
    block_0/block_1 on every page are legitimate;
  * pairing: two lines carrying IDENTICAL boxes = synthetic geometry →
    trusted, not rejected;
  * pipeline: duplicate reverts extend to the hyphen partner (no mixed
    OCR+corrected pair) and page seams are checked too.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from corrigenda.core.pairing import link_hyphen_pairs
from corrigenda.core.planner import plan_page
from corrigenda.core.schemas import (
    ChunkGranularity,
    ChunkPlannerConfig,
    Coords,
    HyphenRole,
    LineManifest,
    LineStatus,
)
from corrigenda.errors import DuplicateIdError
from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.page.parser import parse_page_file

from tests.test_planner_budget_and_cross_chunk_guard import _chain, _line, _page

# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def test_window_walk_survives_validation_bypass():
    """model_copy(update=…) bypasses the P2-5 validator; without the
    progress clamp this spun forever (reproduced before the fix)."""
    cfg = ChunkPlannerConfig().model_copy(
        update={"line_window_size": 8, "line_window_overlap": 8}
    )
    lines = [_line(i, "abc") for i in range(20)]
    plan = plan_page(_page(lines), "d1", cfg, force_granularity=ChunkGranularity.WINDOW)
    covered = {lid for c in plan.chunks for lid in c.targets()}
    assert covered == {lm.line_id for lm in lines}


def test_line_mode_cap_unlinks_the_cut_pair():
    """A chain longer than the cap is truncated — but the pair straddling
    the cut must be UNLINKED so no still-linked pair spans two chunks
    (pair atomicity, CLAUDE.md)."""
    lines = [_line(i, f"mot{i}-") for i in range(12)]
    _chain(lines)
    cfg = ChunkPlannerConfig(
        max_input_chars_per_request=10_000,
        max_lines_per_request=5,
        line_window_size=3,
        line_window_overlap=1,
    )
    plan = plan_page(_page(lines), "d1", cfg, force_granularity=ChunkGranularity.LINE)
    by_id = {lm.line_id: lm for lm in lines}
    for chunk in plan.chunks:
        ids = set(chunk.line_ids)
        for lid in ids:
            lm = by_id[lid]
            for pid in (lm.hyphen_pair_line_id, lm.hyphen_forward_pair_id):
                assert pid is None or pid in ids, (
                    f"{lid} still linked to {pid} outside its chunk"
                )


# ---------------------------------------------------------------------------
# ALTO parser — IDNEXT robustness + margins
# ---------------------------------------------------------------------------


def _alto_doc(page_body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="2000" HEIGHT="3000">
      {page_body}
    </Page>
  </Layout>
</alto>"""


def _tb(block_id: str, content: str, idnext: str | None = None, vpos: int = 10) -> str:
    nxt = f' IDNEXT="{idnext}"' if idnext else ""
    ident = f' ID="{block_id}"' if block_id is not None else ""
    return (
        f'<TextBlock{ident}{nxt} HPOS="10" VPOS="{vpos}" '
        'WIDTH="900" HEIGHT="40">'
        f'<TextLine ID="TL_{block_id or "anon"}_{vpos}" HPOS="10" VPOS="{vpos}" '
        'WIDTH="900" HEIGHT="20">'
        f'<String CONTENT="{content}" HPOS="10" VPOS="{vpos}" WIDTH="900" HEIGHT="20"/>'
        "</TextLine></TextBlock>"
    )


def _write(tmp_path: Path, xml: str, name: str = "t.xml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(xml).strip(), encoding="utf-8")
    return p


def test_alto_empty_string_block_id_does_not_crash(tmp_path: Path):
    """ID=\"\" used to KeyError the IDNEXT chain walk."""
    body = (
        "<PrintSpace>"
        + _tb("B1", "un", idnext="B2", vpos=10)
        + _tb("", "sans id", vpos=50)
        + _tb("B2", "deux", vpos=90)
        + "</PrintSpace>"
    )
    pages, _ = parse_alto_file(_write(tmp_path, _alto_doc(body)), "t.xml")
    texts = [lm.ocr_text for lm in pages[0].lines]
    assert sorted(texts) == sorted(["un", "sans id", "deux"])


def test_alto_idnext_to_next_page_ends_chain_without_voiding_order(tmp_path: Path):
    """A cross-page IDNEXT (valid METS/ALTO continuation) must be treated
    as end-of-chain — the rest of the page's declared order is KEPT
    (before the fix the whole declaration fell back to document order)."""
    body = (
        "<PrintSpace>"
        + _tb("B1", "premier", idnext="B3", vpos=10)
        + _tb("B2", "troisieme", idnext="NEXT_PAGE_BLOCK", vpos=50)
        + _tb("B3", "deuxieme", idnext="B2", vpos=90)
        + "</PrintSpace>"
    )
    pages, _ = parse_alto_file(_write(tmp_path, _alto_doc(body)), "t.xml")
    assert [lm.ocr_text for lm in pages[0].lines] == [
        "premier",
        "deuxieme",
        "troisieme",
    ]


def test_alto_margin_blocks_stay_out_of_scope_without_printspace(tmp_path: Path):
    """No PrintSpace: the whole Page is the container, but margin-nested
    blocks (running heads, page numbers) must remain excluded — the
    historical direct-children lookup excluded them implicitly."""
    body = (
        "<TopMargin>"
        + _tb("M1", "titre courant", vpos=5)
        + "</TopMargin>"
        + _tb("B1", "corps du texte", vpos=100)
    )
    pages, _ = parse_alto_file(_write(tmp_path, _alto_doc(body)), "t.xml")
    assert [lm.ocr_text for lm in pages[0].lines] == ["corps du texte"]


def test_alto_duplicate_margin_line_id_refused_at_parse_time(tmp_path: Path):
    """A margin TextLine reusing a body line's ID used to pass the parse
    gate (manifest scope) and explode only at rewrite time."""
    body = (
        "<TopMargin>"
        '<TextBlock ID="M1" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="30">'
        '<TextLine ID="SHARED" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="20">'
        '<String CONTENT="marge" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="20"/>'
        "</TextLine></TextBlock></TopMargin>"
        "<PrintSpace>"
        '<TextBlock ID="B1" HPOS="0" VPOS="100" WIDTH="900" HEIGHT="30">'
        '<TextLine ID="SHARED" HPOS="0" VPOS="100" WIDTH="900" HEIGHT="20">'
        '<String CONTENT="corps" HPOS="0" VPOS="100" WIDTH="900" HEIGHT="20"/>'
        "</TextLine></TextBlock></PrintSpace>"
    )
    with pytest.raises(DuplicateIdError):
        parse_alto_file(_write(tmp_path, _alto_doc(body)), "t.xml")


# ---------------------------------------------------------------------------
# Identity — block scope
# ---------------------------------------------------------------------------


def test_block_ids_may_repeat_across_pages_of_one_file(tmp_path: Path):
    """Per-page OCR tools reuse block_0/block_1 on every page — block
    lookups are page-scoped downstream, so this is legitimate."""
    xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000"><PrintSpace>
      <TextBlock ID="block_0" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="30">
        <TextLine ID="p1l1" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="20">
          <String CONTENT="un" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="20"/>
        </TextLine>
      </TextBlock>
    </PrintSpace></Page>
    <Page ID="P2" WIDTH="1000" HEIGHT="1000"><PrintSpace>
      <TextBlock ID="block_0" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="30">
        <TextLine ID="p2l1" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="20">
          <String CONTENT="deux" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="20"/>
        </TextLine>
      </TextBlock>
    </PrintSpace></Page>
  </Layout>
</alto>"""
    pages, _ = parse_alto_file(_write(tmp_path, xml), "t.xml")
    assert len(pages) == 2  # parses fine — no DuplicateIdError


# ---------------------------------------------------------------------------
# Pairing — synthetic geometry
# ---------------------------------------------------------------------------


def test_identical_boxes_are_treated_as_synthetic_geometry():
    """Block coords copied onto every line (a common lazy export) must not
    silently disable hyphen pairing."""

    def lm(lid: str) -> LineManifest:
        return LineManifest(
            line_id=lid,
            page_id="p1",
            block_id="b1",
            line_order_global=0,
            line_order_in_block=0,
            coords=Coords(hpos=0, vpos=100, width=800, height=200),
            ocr_text="mot coupe-",
        )

    part1, part2 = lm("l1"), lm("l2")
    part1.hyphen_role = HyphenRole.PART1
    link_hyphen_pairs([part1, part2])
    assert part1.hyphen_pair_line_id == "l2"


# ---------------------------------------------------------------------------
# PAGE — whole-tree duplicate gate
# ---------------------------------------------------------------------------


def test_page_duplicate_nested_line_id_refused(tmp_path: Path):
    xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 100,0 100,50 0,50"/>
      <TextLine id="SHARED"><Coords points="0,0 100,0 100,20 0,20"/>
        <TextEquiv><Unicode>a</Unicode></TextEquiv></TextLine>
      <TextRegion id="r1a">
        <Coords points="0,0 100,0 100,50 0,50"/>
        <TextLine id="SHARED"><Coords points="0,25 100,25 100,45 0,45"/>
          <TextEquiv><Unicode>b</Unicode></TextEquiv></TextLine>
      </TextRegion>
    </TextRegion>
  </Page>
</PcGts>"""
    with pytest.raises(DuplicateIdError):
        parse_page_file(_write(tmp_path, xml), "t.xml")


# ---------------------------------------------------------------------------
# Pipeline — pair-atomic duplicate revert
# ---------------------------------------------------------------------------


def test_duplicate_revert_extends_to_hyphen_partner():
    """Reverting one member of a reconciled pair used to leave a mixed
    OCR+corrected pair — the exact state reconcile_hyphen_pair forbids."""
    from corrigenda.core.pipeline import CorrectionPipeline
    from tests._pipeline_harness import DictProvider, RecordingObserver, _NoopWriter

    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
    )
    part1 = _line(0, "mot coupe-")
    part2 = _line(1, "suite du mot")
    part1.hyphen_role = HyphenRole.PART1
    part1.hyphen_pair_line_id = part2.line_id
    part2.hyphen_role = HyphenRole.PART2
    part2.hyphen_pair_line_id = part1.line_id
    part1.corrected_text = "mot coupé-"
    part2.corrected_text = "suite du mot"
    line_by_id = {lm.line_id: lm for lm in (part1, part2)}

    pipeline._apply_duplicate_reverts(
        reverts={part2.line_id: "adjacent_duplicate_detected"},
        traces=None,
        line_by_id=line_by_id,
    )
    # Both sides reverted — no mixed pair survives.
    assert part2.corrected_text == part2.ocr_text
    assert part1.corrected_text == part1.ocr_text


def test_duplicate_revert_extends_to_CROSS_PAGE_hyphen_partner():
    """Audit P1 — a hyphen pair straddling a PAGE boundary must revert
    atomically too. The flagged member's partner lives on another page,
    absent from the page-local ``line_by_id``; the page-qualified
    ``cross_page_partners`` index is the only way to reach it. Before the
    fix the page-local ``pid in line_by_id`` guard silently skipped it,
    leaving the reconciled cross-page pair half OCR / half corrected."""
    from corrigenda.core.pipeline import CorrectionPipeline
    from tests._pipeline_harness import DictProvider, RecordingObserver, _NoopWriter

    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
    )
    part1 = _line(0, "mot coupe-")
    part2 = _line(1, "suite du mot")
    # PART1 on page A (last line), PART2 on page B (first line).
    part1.page_id = "pA"
    part2.page_id = "pB"
    part1.hyphen_role = HyphenRole.PART1
    part1.hyphen_pair_line_id = part2.line_id
    part1.hyphen_pair_page_id = "pB"
    part2.hyphen_role = HyphenRole.PART2
    part2.hyphen_pair_line_id = part1.line_id
    part2.hyphen_pair_page_id = "pA"
    part1.corrected_text = "mot coupé-"
    part2.corrected_text = "suite du mot"

    # The page-local index the seam/page pass would hold contains only the
    # flagged page's line; the partner is reachable solely via the
    # page-qualified cross-page index.
    line_by_id = {part1.line_id: part1}
    cross_page_partners = {(part2.page_id, part2.line_id): part2}

    pipeline._apply_duplicate_reverts(
        reverts={part1.line_id: "adjacent_duplicate_detected"},
        traces=None,
        line_by_id=line_by_id,
        cross_page_partners=cross_page_partners,
    )
    # Both members reverted despite the partner living on another page.
    assert part1.corrected_text == part1.ocr_text
    assert part2.corrected_text == part2.ocr_text
    assert part2.status is LineStatus.FALLBACK
