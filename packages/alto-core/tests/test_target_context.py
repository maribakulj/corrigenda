"""Target vs context lines in overlapping windows (spec F8).

Pre-F8 an overlap line was corrected in whichever window ran first (its
in-chunk context there was truncated). F8 makes each line a target in
exactly one window — its last (best-following-context) window — so
overlaps become pure context elsewhere, and hyphen pairs never straddle a
target boundary.
"""

from __future__ import annotations

from alto_core.pipeline.chunk_planner import plan_page
from alto_core.schemas import (
    ChunkGranularity,
    ChunkPlannerConfig,
    Coords,
    HyphenRole,
    LineManifest,
    PageManifest,
)


def _line(i: int, **kw) -> LineManifest:
    return LineManifest(
        line_id=f"L{i}",
        page_id="P1",
        block_id="B1",
        line_order_global=i,
        line_order_in_block=i,
        coords=Coords(hpos=0, vpos=i * 10, width=100, height=8),
        ocr_text=f"line {i}",
        **kw,
    )


def _page(lines: list[LineManifest]) -> PageManifest:
    return PageManifest(
        page_id="P1",
        source_file="x.xml",
        page_index=0,
        page_width=100,
        page_height=1000,
        blocks=[],
        lines=lines,
    )


_CFG = ChunkPlannerConfig(line_window_size=5, line_window_overlap=1)


def test_every_line_is_a_target_exactly_once():
    lines = [_line(i) for i in range(23)]
    plan = plan_page(
        _page(lines), "doc", _CFG, force_granularity=ChunkGranularity.WINDOW
    )
    assert plan.granularity == ChunkGranularity.WINDOW
    assert len(plan.chunks) > 1  # multiple overlapping windows

    seen: list[str] = []
    for c in plan.chunks:
        assert c.target_line_ids is not None
        # targets are a subset of the window's lines
        assert set(c.target_line_ids) <= set(c.line_ids)
        seen.extend(c.target_line_ids)

    # exactly once, and covering every line
    assert sorted(seen) == sorted(lm.line_id for lm in lines)
    assert len(seen) == len(set(seen)), "a line was targeted by two windows"


def test_overlap_line_is_context_in_earlier_window():
    lines = [_line(i) for i in range(12)]
    plan = plan_page(
        _page(lines), "doc", _CFG, force_granularity=ChunkGranularity.WINDOW
    )
    # The shared boundary line between window 0 and window 1 must be a
    # target of the LATER window only.
    w0, w1 = plan.chunks[0], plan.chunks[1]
    shared = set(w0.line_ids) & set(w1.line_ids)
    assert shared, "expected an overlap between consecutive windows"
    for lid in shared:
        assert lid not in (w0.target_line_ids or [])
        assert lid in (w1.target_line_ids or [])


def test_hyphen_pair_stays_in_one_target_window():
    """A PART1/PART2 pair on a window boundary must be targeted together."""
    lines = [_line(i) for i in range(12)]
    # Make L4 -> L5 a hyphen pair (L4 is a window boundary at size 5).
    lines[4].hyphen_role = HyphenRole.PART1
    lines[4].hyphen_pair_line_id = "L5"
    lines[5].hyphen_role = HyphenRole.PART2
    lines[5].hyphen_pair_line_id = "L4"

    plan = plan_page(
        _page(lines), "doc", _CFG, force_granularity=ChunkGranularity.WINDOW
    )

    def target_window(lid: str) -> int:
        for i, c in enumerate(plan.chunks):
            if lid in (c.target_line_ids or []):
                return i
        raise AssertionError(f"{lid} never targeted")

    assert target_window("L4") == target_window("L5")


# ---------------------------------------------------------------------------
# End-to-end: a WINDOW-planned run finalizes every line exactly once
# ---------------------------------------------------------------------------


import pytest  # noqa: E402

from alto_core import CorrectionPipeline  # noqa: E402
from alto_core.schemas import DocumentManifest, LineStatus  # noqa: E402


class _IdentityProvider:
    async def list_models(self, api_key):
        return []

    async def complete_structured(self, **kw):
        payload = kw["user_payload"]
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in payload.get("lines", [])
            ]
        }


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, **k):
        pass

    def write_trace(self, **k):
        pass


@pytest.mark.asyncio
async def test_window_run_finalizes_every_line_once():
    lines = [_line(i) for i in range(15)]
    doc = DocumentManifest(
        source_files=["x.xml"],
        pages=[_page(lines)],
        total_pages=1,
        total_blocks=0,
        total_lines=15,
    )
    # Force WINDOW: empty blocks + a tiny line budget defeats PAGE and BLOCK.
    cfg = ChunkPlannerConfig(
        max_lines_per_request=5, line_window_size=5, line_window_overlap=1
    )
    pipeline = CorrectionPipeline(
        provider=_IdentityProvider(),
        observer=_Null(),
        output_writer=_Null(),
        config=cfg,
    )
    await pipeline.run(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={},  # skip XML rewrite; inspect manifests directly
    )
    for lm in doc.pages[0].lines:
        assert lm.status in (LineStatus.CORRECTED, LineStatus.FALLBACK), (
            f"{lm.line_id} left {lm.status} (never targeted)"
        )
        assert lm.corrected_text is not None
