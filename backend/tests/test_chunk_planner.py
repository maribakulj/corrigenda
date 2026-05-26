"""Tests for jobs/chunk_planner.py"""

from __future__ import annotations

from pathlib import Path

import pytest
from alto_core.pipeline.chunk_planner import downgrade_granularity, plan_page

from app.schemas import (
    BlockManifest,
    ChunkGranularity,
    ChunkPlannerConfig,
    Coords,
    HyphenRole,
    LineManifest,
    PageManifest,
)

X0000002_PATH = Path(__file__).parent.parent.parent / "examples" / "X0000002.xml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coords() -> Coords:
    return Coords(hpos=0, vpos=0, width=100, height=20)


def _line(
    line_id: str,
    block_id: str,
    ocr_text: str = "hello",
    hyphen_role: HyphenRole = HyphenRole.NONE,
    hyphen_pair_line_id: str | None = None,
    hyphen_forward_pair_id: str | None = None,
    line_order_global: int = 0,
) -> LineManifest:
    return LineManifest(
        line_id=line_id,
        page_id="P1",
        block_id=block_id,
        line_order_global=line_order_global,
        line_order_in_block=0,
        coords=_coords(),
        ocr_text=ocr_text,
        hyphen_role=hyphen_role,
        hyphen_pair_line_id=hyphen_pair_line_id,
        hyphen_forward_pair_id=hyphen_forward_pair_id,
    )


def _block(block_id: str, line_ids: list[str]) -> BlockManifest:
    return BlockManifest(
        block_id=block_id,
        page_id="P1",
        block_order=0,
        coords=_coords(),
        line_ids=line_ids,
    )


def _page(lines: list[LineManifest], blocks: list[BlockManifest] | None = None) -> PageManifest:
    if blocks is None:
        blocks = [_block("TB1", [lm.line_id for lm in lines])]
    return PageManifest(
        page_id="P1",
        source_file="test.xml",
        page_index=0,
        page_width=2480,
        page_height=3508,
        blocks=blocks,
        lines=lines,
    )


def _small_config() -> ChunkPlannerConfig:
    return ChunkPlannerConfig(
        max_input_chars_per_request=12000,
        max_lines_per_request=80,
        line_window_size=4,
        line_window_overlap=1,
    )


# ---------------------------------------------------------------------------
# test_small_page_single_chunk
# ---------------------------------------------------------------------------


def test_small_page_single_chunk():
    lines = [_line(f"L{i}", "TB1", ocr_text="ab") for i in range(5)]
    page = _page(lines)
    plan = plan_page(page, "DOC1", _small_config())

    assert plan.granularity == ChunkGranularity.PAGE
    assert len(plan.chunks) == 1
    assert set(plan.chunks[0].line_ids) == {lm.line_id for lm in lines}


# ---------------------------------------------------------------------------
# test_large_page_block_granularity
# ---------------------------------------------------------------------------


def test_large_page_block_granularity():
    # 3 blocks of 2 short lines each; each block fits in tight budget (≤20 chars, ≤3 lines)
    # but total page (12 chars total, 6 lines) would exceed max_lines_per_request=3
    config = ChunkPlannerConfig(
        max_input_chars_per_request=20,
        max_lines_per_request=3,
        line_window_size=4,
        line_window_overlap=1,
    )
    lines_b1 = [_line("L1", "B1", "ab"), _line("L2", "B1", "cd")]
    lines_b2 = [_line("L3", "B2", "ef"), _line("L4", "B2", "gh")]
    lines_b3 = [_line("L5", "B3", "ij"), _line("L6", "B3", "kl")]
    all_lines = lines_b1 + lines_b2 + lines_b3
    blocks = [
        _block("B1", ["L1", "L2"]),
        _block("B2", ["L3", "L4"]),
        _block("B3", ["L5", "L6"]),
    ]
    page = _page(all_lines, blocks)
    plan = plan_page(page, "DOC1", config)

    assert plan.granularity == ChunkGranularity.BLOCK
    assert len(plan.chunks) == 3
    covered = {lid for c in plan.chunks for lid in c.line_ids}
    assert covered == {lm.line_id for lm in all_lines}


# ---------------------------------------------------------------------------
# test_block_too_large_window_fallback
# ---------------------------------------------------------------------------


def test_block_too_large_window_fallback():
    config = ChunkPlannerConfig(
        max_input_chars_per_request=10,
        max_lines_per_request=2,
        line_window_size=3,
        line_window_overlap=1,
    )
    lines = [_line(f"L{i}", "TB1", "long line text") for i in range(6)]
    page = _page(lines)
    plan = plan_page(page, "DOC1", config)

    assert plan.granularity == ChunkGranularity.WINDOW
    assert len(plan.chunks) >= 2


# ---------------------------------------------------------------------------
# test_window_coverage_complete
# ---------------------------------------------------------------------------


def test_window_coverage_complete():
    config = ChunkPlannerConfig(
        max_input_chars_per_request=10,
        max_lines_per_request=2,
        line_window_size=3,
        line_window_overlap=1,
    )
    lines = [_line(f"L{i}", "TB1", "text") for i in range(10)]
    page = _page(lines)
    plan = plan_page(page, "DOC1", config)

    assert plan.granularity == ChunkGranularity.WINDOW
    covered = {lid for c in plan.chunks for lid in c.line_ids}
    assert covered == {lm.line_id for lm in lines}


# ---------------------------------------------------------------------------
# test_hyphen_pair_not_split_by_window
# ---------------------------------------------------------------------------


def test_hyphen_pair_not_split_by_window():
    # window_size=3, overlap=1, step=2
    # Lines: L0 L1 L2(PART1→L3) L3(PART2) L4 L5
    # Window 0 = [L0,L1,L2] → L2 is PART1 at end → extend → [L0,L1,L2,L3]
    config = ChunkPlannerConfig(
        max_input_chars_per_request=10,
        max_lines_per_request=2,
        line_window_size=3,
        line_window_overlap=1,
    )
    lines = [
        _line("L0", "TB1", "aa"),
        _line("L1", "TB1", "bb"),
        _line("L2", "TB1", "cc", HyphenRole.PART1, "L3"),
        _line("L3", "TB1", "dd", HyphenRole.PART2, "L2"),
        _line("L4", "TB1", "ee"),
        _line("L5", "TB1", "ff"),
    ]
    page = _page(lines)
    plan = plan_page(page, "DOC1", config)

    assert plan.granularity == ChunkGranularity.WINDOW
    for chunk in plan.chunks:
        if "L2" in chunk.line_ids:
            assert "L3" in chunk.line_ids, f"PART1 L2 and PART2 L3 were split: {chunk.line_ids}"


# ---------------------------------------------------------------------------
# test_hyphen_pair_atomic_in_line_mode
# ---------------------------------------------------------------------------


def test_hyphen_pair_atomic_in_line_mode():
    lines = [
        _line("L0", "TB1", "aa"),
        _line("L1", "TB1", "bb", HyphenRole.PART1, "L2"),
        _line("L2", "TB1", "cc", HyphenRole.PART2, "L1"),
        _line("L3", "TB1", "dd"),
    ]
    page = _page(lines)
    plan = plan_page(page, "DOC1", _small_config(), force_granularity=ChunkGranularity.LINE)

    assert plan.granularity == ChunkGranularity.LINE
    for chunk in plan.chunks:
        if "L1" in chunk.line_ids:
            assert "L2" in chunk.line_ids, (
                f"L1 (PART1) and L2 (PART2) must be atomic: {chunk.line_ids}"
            )
    l0_chunks = [c for c in plan.chunks if "L0" in c.line_ids]
    assert len(l0_chunks) == 1 and len(l0_chunks[0].line_ids) == 1


# ---------------------------------------------------------------------------
# test_downgrade_sequence
# ---------------------------------------------------------------------------


def test_downgrade_sequence():
    assert downgrade_granularity(ChunkGranularity.PAGE) == ChunkGranularity.BLOCK
    assert downgrade_granularity(ChunkGranularity.BLOCK) == ChunkGranularity.WINDOW
    assert downgrade_granularity(ChunkGranularity.WINDOW) == ChunkGranularity.LINE
    assert downgrade_granularity(ChunkGranularity.LINE) is None


# ---------------------------------------------------------------------------
# test_chain_atomic_in_line_mode (PART1 → BOTH → PART2)
# ---------------------------------------------------------------------------


def test_chain_atomic_in_line_mode():
    """A chain PART1 → BOTH → PART2 must be grouped as a single chunk."""
    lines = [
        _line("L0", "TB1", "aa"),
        _line("L1", "TB1", "néces-", HyphenRole.PART1, hyphen_pair_line_id="L2"),
        _line(
            "L2",
            "TB1",
            "saires pour les me-",
            HyphenRole.BOTH,
            hyphen_pair_line_id="L1",
            hyphen_forward_pair_id="L3",
        ),
        _line("L3", "TB1", "sures nécessaires", HyphenRole.PART2, hyphen_pair_line_id="L2"),
        _line("L4", "TB1", "dd"),
    ]
    page = _page(lines)
    plan = plan_page(page, "DOC1", _small_config(), force_granularity=ChunkGranularity.LINE)

    assert plan.granularity == ChunkGranularity.LINE

    # Find the chunk containing L1
    chain_chunk = [c for c in plan.chunks if "L1" in c.line_ids]
    assert len(chain_chunk) == 1
    assert set(chain_chunk[0].line_ids) == {"L1", "L2", "L3"}, (
        f"Chain PART1→BOTH→PART2 not grouped: {chain_chunk[0].line_ids}"
    )

    # L0 and L4 should be separate single-line chunks
    l0_chunk = [c for c in plan.chunks if "L0" in c.line_ids]
    assert len(l0_chunk) == 1 and len(l0_chunk[0].line_ids) == 1
    l4_chunk = [c for c in plan.chunks if "L4" in c.line_ids]
    assert len(l4_chunk) == 1 and len(l4_chunk[0].line_ids) == 1


# ---------------------------------------------------------------------------
# test_long_chain_atomic_in_line_mode (5-line chain)
# ---------------------------------------------------------------------------


def test_long_chain_atomic_in_line_mode():
    """A 5-line chain PART1 → BOTH → BOTH → BOTH → PART2 stays together."""
    lines = [
        _line("L0", "TB1", "aa"),
        _line("A", "TB1", "pre et d'or, aux la-", HyphenRole.PART1, hyphen_pair_line_id="B"),
        _line(
            "B",
            "TB1",
            "quais vêtus com-",
            HyphenRole.BOTH,
            hyphen_pair_line_id="A",
            hyphen_forward_pair_id="C",
        ),
        _line(
            "C",
            "TB1",
            "me des princes, aux che-",
            HyphenRole.BOTH,
            hyphen_pair_line_id="B",
            hyphen_forward_pair_id="D",
        ),
        _line(
            "D",
            "TB1",
            "vaux flamands dont les har-",
            HyphenRole.BOTH,
            hyphen_pair_line_id="C",
            hyphen_forward_pair_id="E",
        ),
        _line("E", "TB1", "nais couverts", HyphenRole.PART2, hyphen_pair_line_id="D"),
        _line("L6", "TB1", "zz"),
    ]
    page = _page(lines)
    plan = plan_page(page, "DOC1", _small_config(), force_granularity=ChunkGranularity.LINE)

    chain_chunk = [c for c in plan.chunks if "A" in c.line_ids]
    assert len(chain_chunk) == 1
    assert chain_chunk[0].line_ids == ["A", "B", "C", "D", "E"], (
        f"5-line chain not grouped: {chain_chunk[0].line_ids}"
    )


# ---------------------------------------------------------------------------
# test_chain_not_split_by_window
# ---------------------------------------------------------------------------


def test_chain_not_split_by_window():
    """Window boundary falling inside a chain must extend to include all."""
    # window_size=3, overlap=1, step=2
    # Lines: L0 L1 L2(PART1→L3) L3(BOTH→L4) L4(PART2) L5
    # Window 0 = [L0,L1,L2] → L2 is PART1 → extend → L3 is BOTH → extend → L4
    # Result: [L0,L1,L2,L3,L4]
    config = ChunkPlannerConfig(
        max_input_chars_per_request=10,
        max_lines_per_request=2,
        line_window_size=3,
        line_window_overlap=1,
    )
    lines = [
        _line("L0", "TB1", "aa"),
        _line("L1", "TB1", "bb"),
        _line("L2", "TB1", "néces-", HyphenRole.PART1, hyphen_pair_line_id="L3"),
        _line(
            "L3",
            "TB1",
            "saires me-",
            HyphenRole.BOTH,
            hyphen_pair_line_id="L2",
            hyphen_forward_pair_id="L4",
        ),
        _line("L4", "TB1", "sures", HyphenRole.PART2, hyphen_pair_line_id="L3"),
        _line("L5", "TB1", "ff"),
    ]
    page = _page(lines)
    plan = plan_page(page, "DOC1", config)

    assert plan.granularity == ChunkGranularity.WINDOW
    for chunk in plan.chunks:
        if "L2" in chunk.line_ids:
            assert "L3" in chunk.line_ids and "L4" in chunk.line_ids, (
                f"Chain split at window boundary: {chunk.line_ids}"
            )


# ---------------------------------------------------------------------------
# test_cross_block_chain_merged
# ---------------------------------------------------------------------------


def test_cross_block_chain_merged():
    """BOTH's forward link across blocks triggers block merging."""
    config = ChunkPlannerConfig(
        max_input_chars_per_request=200,
        max_lines_per_request=5,
    )
    lines_b1 = [
        _line("L1", "B1", "text abc-", HyphenRole.PART1, hyphen_pair_line_id="L2"),
        _line(
            "L2",
            "B1",
            "def ghi-",
            HyphenRole.BOTH,
            hyphen_pair_line_id="L1",
            hyphen_forward_pair_id="L3",
        ),
    ]
    lines_b2 = [
        _line("L3", "B2", "jkl rest", HyphenRole.PART2, hyphen_pair_line_id="L2"),
        _line("L4", "B2", "normal"),
    ]
    lines_b3 = [
        _line("L5", "B3", "other block a"),
        _line("L6", "B3", "other block b"),
    ]
    all_lines = lines_b1 + lines_b2 + lines_b3
    blocks = [
        _block("B1", ["L1", "L2"]),
        _block("B2", ["L3", "L4"]),
        _block("B3", ["L5", "L6"]),
    ]
    page = _page(all_lines, blocks)
    plan = plan_page(page, "DOC1", config)

    assert plan.granularity == ChunkGranularity.BLOCK
    # B1 and B2 should be merged into one chunk; B3 separate
    assert len(plan.chunks) == 2
    merged = [c for c in plan.chunks if "L1" in c.line_ids][0]
    assert set(merged.line_ids) == {"L1", "L2", "L3", "L4"}


# ---------------------------------------------------------------------------
# test_corpus_chains_never_split (real XML)
# ---------------------------------------------------------------------------


def test_corpus_chains_never_split():
    """On X0000002.xml, no BOTH line's forward partner is in a different chunk."""
    if not X0000002_PATH.exists():
        pytest.skip("X0000002.xml not available")

    from alto_core.alto.parser import parse_alto_file

    pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")

    config = _small_config()
    for page in pages:
        plan = plan_page(page, "DOC1", config, force_granularity=ChunkGranularity.LINE)

        # Build line_id → chunk_id mapping
        lid_to_chunk = {}
        for chunk in plan.chunks:
            for lid in chunk.line_ids:
                lid_to_chunk[lid] = chunk.chunk_id

        # Verify all BOTH forward links are in the same chunk
        for lm in page.lines:
            if lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id:
                assert lid_to_chunk.get(lm.line_id) == lid_to_chunk.get(
                    lm.hyphen_forward_pair_id
                ), (
                    f"BOTH line {lm.line_id} and forward partner "
                    f"{lm.hyphen_forward_pair_id} are in different chunks"
                )
            if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
                assert lid_to_chunk.get(lm.line_id) == lid_to_chunk.get(lm.hyphen_pair_line_id), (
                    f"PART1 line {lm.line_id} and PART2 partner "
                    f"{lm.hyphen_pair_line_id} are in different chunks"
                )
