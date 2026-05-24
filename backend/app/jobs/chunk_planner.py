"""Chunk planner: splits a page's lines into LLM-sized chunks."""

from __future__ import annotations

import uuid

from app.alto.hyphenation import should_stay_in_same_chunk
from app.schemas import (
    ChunkGranularity,
    ChunkPlan,
    ChunkPlannerConfig,
    ChunkRequest,
    HyphenRole,
    LineManifest,
    PageManifest,
)

# ---------------------------------------------------------------------------
# Granularity downgrade
# ---------------------------------------------------------------------------

_CHAIN = [
    ChunkGranularity.PAGE,
    ChunkGranularity.BLOCK,
    ChunkGranularity.WINDOW,
    ChunkGranularity.LINE,
]


def downgrade_granularity(current: ChunkGranularity) -> ChunkGranularity | None:
    """Return the next granularity level, or None if already at LINE."""
    idx = _CHAIN.index(current)
    if idx + 1 < len(_CHAIN):
        return _CHAIN[idx + 1]
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _total_chars(lines: list[LineManifest]) -> int:
    return sum(len(lm.ocr_text) for lm in lines)


def _make_chunk(
    document_id: str,
    page_id: str,
    granularity: ChunkGranularity,
    line_ids: list[str],
    block_id: str | None = None,
) -> ChunkRequest:
    return ChunkRequest(
        chunk_id=str(uuid.uuid4()),
        document_id=document_id,
        page_id=page_id,
        block_id=block_id,
        granularity=granularity,
        line_ids=list(line_ids),
    )


# ---------------------------------------------------------------------------
# PAGE granularity
# ---------------------------------------------------------------------------


def _try_page(
    page: PageManifest,
    document_id: str,
    config: ChunkPlannerConfig,
) -> ChunkPlan | None:
    lines = page.lines
    if (
        _total_chars(lines) <= config.max_input_chars_per_request
        and len(lines) <= config.max_lines_per_request
    ):
        chunk = _make_chunk(
            document_id,
            page.page_id,
            ChunkGranularity.PAGE,
            [lm.line_id for lm in lines],
        )
        return ChunkPlan(
            page_id=page.page_id,
            chunks=[chunk],
            granularity=ChunkGranularity.PAGE,
        )
    return None


# ---------------------------------------------------------------------------
# BLOCK granularity
# ---------------------------------------------------------------------------


def _try_block(
    page: PageManifest,
    document_id: str,
    config: ChunkPlannerConfig,
) -> ChunkPlan | None:
    line_by_id = {lm.line_id: lm for lm in page.lines}

    # Group lines by block in page.blocks order
    block_lines: dict[str, list[LineManifest]] = {}
    for block in page.blocks:
        block_lines[block.block_id] = [
            line_by_id[lid] for lid in block.line_ids if lid in line_by_id
        ]

    block_ids_ordered = [b.block_id for b in page.blocks]

    # Union-find to merge blocks linked by cross-block hyphen pairs
    parent: dict[str, str] = {bid: bid for bid in block_ids_ordered}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for lm in page.lines:
        if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
            pair = line_by_id.get(lm.hyphen_pair_line_id)
            if pair and pair.block_id != lm.block_id:
                if lm.block_id in parent and pair.block_id in parent:
                    union(lm.block_id, pair.block_id)
        elif lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id:
            pair = line_by_id.get(lm.hyphen_forward_pair_id)
            if pair and pair.block_id != lm.block_id:
                if lm.block_id in parent and pair.block_id in parent:
                    union(lm.block_id, pair.block_id)

    # Collect groups in page order (use dict to deduplicate while preserving order)
    seen_roots: dict[str, None] = {}
    for bid in block_ids_ordered:
        seen_roots[find(bid)] = None

    groups: dict[str, list[str]] = {}
    for bid in block_ids_ordered:
        root = find(bid)
        groups.setdefault(root, []).append(bid)

    chunks: list[ChunkRequest] = []
    for root in seen_roots:
        group_block_ids = groups[root]
        group_lines: list[LineManifest] = []
        for bid in group_block_ids:
            group_lines.extend(block_lines.get(bid, []))

        if (
            _total_chars(group_lines) > config.max_input_chars_per_request
            or len(group_lines) > config.max_lines_per_request
        ):
            return None  # too large → fall back to WINDOW

        block_id_label = group_block_ids[0] if len(group_block_ids) == 1 else None
        chunks.append(
            _make_chunk(
                document_id,
                page.page_id,
                ChunkGranularity.BLOCK,
                [lm.line_id for lm in group_lines],
                block_id=block_id_label,
            )
        )

    if not chunks:
        return None

    return ChunkPlan(
        page_id=page.page_id,
        chunks=chunks,
        granularity=ChunkGranularity.BLOCK,
    )


# ---------------------------------------------------------------------------
# WINDOW granularity
# ---------------------------------------------------------------------------


def _try_window(
    page: PageManifest,
    document_id: str,
    config: ChunkPlannerConfig,
) -> ChunkPlan:
    lines = page.lines
    n = len(lines)
    if n == 0:
        return ChunkPlan(page_id=page.page_id, chunks=[], granularity=ChunkGranularity.WINDOW)

    window_size = config.line_window_size
    overlap = config.line_window_overlap
    step = window_size - overlap

    chunks: list[ChunkRequest] = []
    start = 0

    while start < n:
        end = min(start + window_size, n)  # exclusive

        # Extend to keep hyphen chains intact at window boundary,
        # but cap to avoid unbounded growth beyond the token budget.
        extension_limit = max(config.max_lines_per_request, end - start + 10)
        max_end = min(n, start + extension_limit)
        while end < max_end:
            last_in_window = lines[end - 1]
            next_line = lines[end]
            if should_stay_in_same_chunk(last_in_window, next_line):
                end += 1
            else:
                break

        chunk_line_ids = [lines[i].line_id for i in range(start, end)]
        chunks.append(
            _make_chunk(
                document_id,
                page.page_id,
                ChunkGranularity.WINDOW,
                chunk_line_ids,
            )
        )

        next_start = start + step
        if next_start <= start:
            next_start = start + 1
        start = next_start

    return ChunkPlan(
        page_id=page.page_id,
        chunks=chunks,
        granularity=ChunkGranularity.WINDOW,
    )


# ---------------------------------------------------------------------------
# LINE granularity
# ---------------------------------------------------------------------------


def _plan_line(
    page: PageManifest,
    document_id: str,
    config: ChunkPlannerConfig,
) -> ChunkPlan:
    lines = page.lines
    chunks: list[ChunkRequest] = []
    i = 0
    while i < len(lines):
        lm = lines[i]
        # Follow the full chain: PART1 → BOTH → ... → BOTH → PART2
        # All lines linked by forward hyphen pairs must stay together.
        chain_ids = [lm.line_id]
        j = i
        while j < len(lines):
            cur = lines[j]
            forward_pair = (
                cur.hyphen_pair_line_id
                if cur.hyphen_role == HyphenRole.PART1
                else cur.hyphen_forward_pair_id
                if cur.hyphen_role == HyphenRole.BOTH
                else None
            )
            if forward_pair and j + 1 < len(lines) and lines[j + 1].line_id == forward_pair:
                chain_ids.append(lines[j + 1].line_id)
                j += 1
            else:
                break

        chunks.append(
            _make_chunk(
                document_id,
                page.page_id,
                ChunkGranularity.LINE,
                chain_ids,
            )
        )
        i += len(chain_ids)

    return ChunkPlan(
        page_id=page.page_id,
        chunks=chunks,
        granularity=ChunkGranularity.LINE,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan_page(
    page: PageManifest,
    document_id: str,
    config: ChunkPlannerConfig,
    force_granularity: ChunkGranularity | None = None,
) -> ChunkPlan:
    """
    Produce a ChunkPlan for a page.

    Tries PAGE → BLOCK → WINDOW (→ LINE only when force_granularity=LINE).
    force_granularity skips directly to a specific level.
    """
    if force_granularity == ChunkGranularity.LINE:
        return _plan_line(page, document_id, config)
    if force_granularity == ChunkGranularity.WINDOW:
        return _try_window(page, document_id, config)
    if force_granularity == ChunkGranularity.BLOCK:
        result = _try_block(page, document_id, config)
        if result:
            return result
        return _try_window(page, document_id, config)
    if force_granularity == ChunkGranularity.PAGE:
        result = _try_page(page, document_id, config)
        if result:
            return result
        # fall through auto-select

    # Auto-select: PAGE → BLOCK → WINDOW
    result = _try_page(page, document_id, config)
    if result:
        return result

    result = _try_block(page, document_id, config)
    if result:
        return result

    return _try_window(page, document_id, config)
