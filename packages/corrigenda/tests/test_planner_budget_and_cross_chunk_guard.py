"""P1-8 — real planner budgets · P2-6 — cross-chunk duplication guard.

P1-8: only PAGE and BLOCK honoured ``max_input_chars_per_request``; a
WINDOW of pathologically long lines blew straight past it, and LINE mode
could follow an unbounded hyphen chain — so the knob was not a real
guarantee. Windows are now bounded by both the line count AND the char
budget (with two documented atomic exceptions), the overlap step follows
the actual window so nothing is skipped, and LINE chains are capped.

P2-6: adjacent-duplicate detection ran per chunk on that chunk's target
lines only — two document-adjacent lines owned by different chunks were
never compared, so a duplication straddling a chunk boundary escaped the
guard. A page-level pass now closes the gap.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from corrigenda.core.pipeline import CorrectionPipeline
from corrigenda.core.planner import plan_page
from corrigenda.core.schemas import (
    ChunkGranularity,
    ChunkPlannerConfig,
    Coords,
    GuardConfig,
    HyphenRole,
    LineManifest,
    LineStatus,
    PageManifest,
)
from corrigenda.formats.alto.parser import build_document_manifest

from tests._pipeline_harness import DictProvider, RecordingObserver, _NoopWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line(i: int, text: str, role: HyphenRole = HyphenRole.NONE) -> LineManifest:
    lm = LineManifest(
        line_id=f"l{i}",
        page_id="p1",
        block_id="b1",
        line_order_global=i,
        line_order_in_block=i,
        coords=Coords(hpos=0, vpos=25 * i, width=800, height=20),
        ocr_text=text,
    )
    lm.hyphen_role = role
    return lm


def _page(lines: list[LineManifest]) -> PageManifest:
    return PageManifest(
        page_id="p1",
        source_file="a.xml",
        page_index=0,
        page_width=1000,
        page_height=5000,
        blocks=[],
        lines=lines,
    )


def _chain(lines: list[LineManifest]) -> None:
    """Link consecutive lines as one hyphen chain (PART1→BOTH…→PART2)."""
    for k in range(len(lines) - 1):
        cur, nxt = lines[k], lines[k + 1]
        if k == 0:
            cur.hyphen_role = HyphenRole.PART1
            cur.hyphen_pair_line_id = nxt.line_id
        else:
            cur.hyphen_role = HyphenRole.BOTH
            cur.hyphen_forward_pair_id = nxt.line_id
        # Backward link of the next node (its role is finalised on the
        # next iteration, except the tail which stays PART2).
        nxt.hyphen_role = HyphenRole.PART2
        nxt.hyphen_pair_line_id = cur.line_id


# ---------------------------------------------------------------------------
# P1-8 — WINDOW char budget
# ---------------------------------------------------------------------------


def _window_plan(lines, *, budget=200, window=5, overlap=1):
    cfg = ChunkPlannerConfig(
        max_input_chars_per_request=budget,
        max_lines_per_request=50,
        line_window_size=window,
        line_window_overlap=overlap,
    )
    return plan_page(
        _page(lines), "d1", cfg, force_granularity=ChunkGranularity.WINDOW
    ), cfg


def test_window_respects_char_budget():
    # 10 lines of 80 chars, budget 200 → at most 2 lines per window
    # even though the window size allows 5.
    lines = [_line(i, "x" * 80) for i in range(10)]
    plan, cfg = _window_plan(lines, budget=200, window=5)
    by_id = {lm.line_id: lm for lm in lines}
    for chunk in plan.chunks:
        total = sum(len(by_id[lid].ocr_text) for lid in chunk.line_ids)
        assert total <= cfg.max_input_chars_per_request, (
            f"chunk {chunk.line_ids} carries {total} chars > budget"
        )


def test_budget_shortened_windows_never_skip_lines():
    """The overlap step must follow the ACTUAL window end: with the
    historical fixed step, a budget-shortened window skipped lines."""
    lines = [_line(i, "x" * 80) for i in range(11)]
    plan, _ = _window_plan(lines, budget=200, window=5, overlap=1)
    targeted = [lid for c in plan.chunks for lid in c.targets()]
    assert sorted(targeted) == sorted(lm.line_id for lm in lines)
    assert len(targeted) == len(set(targeted)), "a line is targeted twice"


def test_single_line_over_budget_still_ships_alone():
    lines = [_line(0, "y" * 500), _line(1, "x" * 80)]
    plan, _ = _window_plan(lines, budget=200, window=5)
    assert plan.chunks[0].line_ids[0] == "l0"
    assert len(plan.chunks[0].line_ids) == 1  # atomic, alone, over budget


def test_hyphen_chain_extension_outranks_char_budget():
    # Chain of 3 lines starting at the window boundary: atomicity must win
    # over the char budget (documented exception).
    lines = [_line(i, "x" * 90) for i in range(5)]
    _chain(lines[1:4])
    plan, _ = _window_plan(lines, budget=200, window=2, overlap=0)
    # Find the chunk owning l1: it must also contain l2 and l3.
    owner = next(c for c in plan.chunks if "l1" in c.targets())
    assert {"l1", "l2", "l3"} <= set(owner.line_ids)


def test_full_window_step_is_backwards_compatible():
    # Short lines: char budget never binds → same windows as the
    # historical fixed-step planner (size 5, overlap 1 → starts 0,4,8…).
    lines = [_line(i, "abc") for i in range(12)]
    plan, _ = _window_plan(lines, budget=10_000, window=5, overlap=1)
    firsts = [c.line_ids[0] for c in plan.chunks]
    assert firsts == ["l0", "l4", "l8"]


# ---------------------------------------------------------------------------
# P1-8 — LINE chain cap
# ---------------------------------------------------------------------------


def test_line_mode_caps_unbounded_chains():
    # 30 lines all chained; max_lines_per_request=10 → no chunk may exceed
    # the cap (an adversarial every-line-hyphenated page is not a DoS).
    lines = [_line(i, f"mot{i}-") for i in range(30)]
    _chain(lines)
    cfg = ChunkPlannerConfig(
        max_input_chars_per_request=10_000,
        max_lines_per_request=10,
        line_window_size=5,
        line_window_overlap=1,
    )
    plan = plan_page(_page(lines), "d1", cfg, force_granularity=ChunkGranularity.LINE)
    assert all(len(c.line_ids) <= 10 for c in plan.chunks)
    covered = [lid for c in plan.chunks for lid in c.line_ids]
    assert sorted(covered) == sorted(lm.line_id for lm in lines)


# ---------------------------------------------------------------------------
# P2-6 — cross-chunk duplication guard (real pipeline, end to end)
# ---------------------------------------------------------------------------

_ALTO_8_LINES = """\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="1000">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
          {lines}
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""

_TEXTS = [
    "Il faisait ce jour la un temps splendide",
    "et la lumiere dorait les vieux murs",
    "la riviere descendait vers le moulin",
    "les enfants couraient dans la prairie",
    "un orage montait derriere la colline",
    "le vent se leva soudain sur la plaine",
    "les cloches sonnaient au village voisin",
    "et la nuit tomba sur la campagne calme",
]


def _write_doc(tmp_path: Path) -> Path:
    body = "".join(
        f'<TextLine ID="L{i}" HPOS="10" VPOS="{30 * i + 10}" WIDTH="900" HEIGHT="20">'
        f'<String CONTENT="{t}" HPOS="10" VPOS="{30 * i + 10}" WIDTH="900" HEIGHT="20"/>'
        "</TextLine>"
        for i, t in enumerate(_TEXTS)
    )
    p = tmp_path / "doc.xml"
    p.write_text(textwrap.dedent(_ALTO_8_LINES).format(lines=body), encoding="utf-8")
    return p


def test_cross_chunk_adjacent_duplicate_is_reverted(tmp_path: Path):
    """L3 and L4 are adjacent in the document but owned by DIFFERENT
    window chunks (window 4, overlap 0 → targets {L0..L3} and {L4..L7}).
    The producer returns the same hallucinated sentence for both — the
    classic boundary migration the per-chunk guard could never see."""
    path = _write_doc(tmp_path)
    doc = build_document_manifest([(path, "doc.xml")])

    dup = "le meme texte hallucine identique pour deux lignes"
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({"L3": dup, "L4": dup}),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
        config=ChunkPlannerConfig(
            max_input_chars_per_request=200,  # force WINDOW granularity
            max_lines_per_request=50,
            line_window_size=4,
            line_window_overlap=0,
        ),
        # Stage C neutralised: line-level similarity/neighbour guards must
        # not eat the scenario first — the ONLY protection left against
        # the boundary duplicate is the (page-level) duplicates guard,
        # which is exactly the seam under test.
        guard_config=GuardConfig(min_source_similarity=0.0, neighbour_margin=1.0),
    )
    result = pipeline.run_sync(
        document_manifest=doc,
        source_files={"doc.xml": path},
        apply=False,
    )

    lines = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    # Sanity: the two lines really were split across chunks — the per-chunk
    # guard alone could not have compared them (asserted via reverts below).
    assert lines["L3"].corrected_text == lines["L3"].ocr_text
    assert lines["L4"].corrected_text == lines["L4"].ocr_text
    assert lines["L3"].status == LineStatus.FALLBACK
    assert lines["L4"].status == LineStatus.FALLBACK
    reasons = {
        t.line_id: t.fallback_reason
        for t in result.report.lines
        if t.line_id in ("L3", "L4")
    }
    assert reasons == {
        "L3": "adjacent_duplicate_detected",
        "L4": "adjacent_duplicate_detected",
    }


def test_intra_chunk_duplicates_still_reverted(tmp_path: Path):
    """Regression guard: the page-level pass must not replace the
    per-chunk behaviour — duplicates inside one chunk keep reverting."""
    path = _write_doc(tmp_path)
    doc = build_document_manifest([(path, "doc.xml")])
    dup = "le meme texte hallucine identique pour deux lignes"
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({"L1": dup, "L2": dup}),  # same window (L0..L3)
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
        config=ChunkPlannerConfig(
            max_input_chars_per_request=200,
            max_lines_per_request=50,
            line_window_size=4,
            line_window_overlap=0,
        ),
        guard_config=GuardConfig(min_source_similarity=0.0, neighbour_margin=1.0),
    )
    pipeline.run_sync(
        document_manifest=doc, source_files={"doc.xml": path}, apply=False
    )
    lines = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    assert lines["L1"].corrected_text == lines["L1"].ocr_text
    assert lines["L2"].corrected_text == lines["L2"].ocr_text


def _write_two_line_doc(tmp_path: Path, name: str, texts: list[str]) -> Path:
    body = "".join(
        f'<TextLine ID="{name}_L{i}" HPOS="10" VPOS="{30 * i + 10}" '
        f'WIDTH="900" HEIGHT="20">'
        f'<String CONTENT="{t}" HPOS="10" VPOS="{30 * i + 10}" '
        f'WIDTH="900" HEIGHT="20"/></TextLine>'
        for i, t in enumerate(texts)
    )
    p = tmp_path / f"{name}.xml"
    p.write_text(textwrap.dedent(_ALTO_8_LINES).format(lines=body), encoding="utf-8")
    return p


def test_page_seam_duplicate_not_reverted_across_DIFFERENT_files(tmp_path: Path):
    """Audit P2 — the page-seam duplicate pass must compare a seam ONLY
    within one source file. Two files are concatenated in the document
    manifest, so the last physical line of file A sits document-adjacent
    to the first line of file B — but they are NOT visually adjacent, and
    an identical correction across that seam is a genuine coincidence, not
    a hallucinated duplication. Without the same-file guard the guard
    spuriously reverted one of them."""
    # Distinct line_ids across files (A_L*, B_L*) so the seam_map uniqueness
    # guard does NOT fire — the ONLY thing that can prevent the false revert
    # here is the source-file guard under test.
    file_a = _write_two_line_doc(tmp_path, "A", ["alpha un", "source distincte A"])
    file_b = _write_two_line_doc(tmp_path, "B", ["source distincte B", "beta deux"])
    doc = build_document_manifest([(file_a, "A.xml"), (file_b, "B.xml")])

    dup = "meme correction identique de part et dautre du joint"
    # A_L1 is the LAST line of page A; B_L0 is the FIRST line of page B.
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({"A_L1": dup, "B_L0": dup}),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
        guard_config=GuardConfig(min_source_similarity=0.0, neighbour_margin=1.0),
    )
    pipeline.run_sync(
        document_manifest=doc,
        source_files={"A.xml": file_a, "B.xml": file_b},
        apply=False,
    )
    lines = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    # Neither seam line was reverted: the correction stands, status CORRECTED.
    assert lines["A_L1"].corrected_text == dup
    assert lines["B_L0"].corrected_text == dup
    assert lines["A_L1"].status == LineStatus.CORRECTED
    assert lines["B_L0"].status == LineStatus.CORRECTED


# ---------------------------------------------------------------------------
# Audit P0 — 3+ line hyphen chain must be targeted in ONE window
# ---------------------------------------------------------------------------


def test_multiline_hyphen_chain_targeted_in_single_window():
    """A chain L0=PART1 -> L1=BOTH -> L2=PART2 (two consecutive hyphenated
    words) must have all three lines as targets of the SAME window — the
    pairwise last-write-wins pin used to split (L0,L1) across two chunks,
    corrupting the join order-dependently."""
    # Surround the chain so windows overlap around it.
    lines = [_line(0, "avant")]
    chain = [_line(1, "mot1-"), _line(2, "mot2-"), _line(3, "fin")]
    _chain(chain)
    lines += chain + [_line(4, "apres")]
    for i, lm in enumerate(lines):
        lm.line_order_global = i
        lm.line_order_in_block = i
    cfg = ChunkPlannerConfig(
        max_input_chars_per_request=10_000,
        max_lines_per_request=50,
        line_window_size=3,
        line_window_overlap=1,
    )
    plan = plan_page(_page(lines), "d1", cfg, force_granularity=ChunkGranularity.WINDOW)
    # Every line is a target of exactly one chunk (F8).
    owner = {}
    for ci, c in enumerate(plan.chunks):
        for lid in c.targets():
            assert lid not in owner, f"{lid} targeted by two chunks"
            owner[lid] = ci
    # The three chain members share one target chunk.
    assert owner["l1"] == owner["l2"] == owner["l3"], (
        f"chain split across chunks: {[owner.get(x) for x in ('l1', 'l2', 'l3')]}"
    )
