"""Chunk planner: splits a page's lines into LLM-sized chunks.

Budget semantics: ``max_input_chars_per_request`` bounds the sum of
the chunk lines' RAW OCR text — it deliberately excludes the JSON
envelope, system prompt, neighbour context and optional geometry the
enrichment step adds (all of which grow roughly linearly with the same
line count). Consumers sizing the budget against a provider's token limit
should keep generous headroom (the 12 000 default assumes ~3-4× overhead
against 128k-class context windows). Two documented exceptions may
overshoot the budget, both bounded by ``max_lines_per_request``:

  * a hyphen CHAIN is atomic — splitting a hyphenated word across chunks
    corrupts reconciliation, so chain extension outranks the char budget;
  * a single line longer than the whole budget still ships alone (a line
    is the smallest unit the pipeline corrects).
"""

from __future__ import annotations

import uuid

from corrigenda.core.hyphenation import should_stay_in_same_chunk
from corrigenda.core.pairing import forward_partner_id
from corrigenda.core.schemas import (
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
    target_line_ids: list[str] | None = None,
) -> ChunkRequest:
    return ChunkRequest(
        chunk_id=str(uuid.uuid4()),
        document_id=document_id,
        page_id=page_id,
        block_id=block_id,
        granularity=granularity,
        line_ids=list(line_ids),
        target_line_ids=None if target_line_ids is None else list(target_line_ids),
    )


def _hyphen_partner_id(lm: LineManifest) -> str | None:
    """Return the forward/backward hyphen partner line_id, if any."""
    if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.PART2):
        return lm.hyphen_pair_line_id
    if lm.hyphen_role == HyphenRole.BOTH:
        # A BOTH line pairs backward (hyphen_pair_line_id) and forward
        # (hyphen_forward_pair_id); either partner is enough to keep the
        # chain in one target window (chains are contiguous).
        return lm.hyphen_forward_pair_id or lm.hyphen_pair_line_id
    return None


def _assign_window_targets(
    windows: list[list[str]],
    line_by_id: dict[str, LineManifest],
) -> list[list[str]]:
    """Assign every line to exactly one target window (F8).

    Overlapping windows mean a boundary line appears in two windows; pre-F8
    it was corrected in whichever window ran first (its context there was
    truncated). Here each line becomes a target in the LAST window that
    contains it — the window where it has the most in-chunk *following*
    context (following lines drive word-completion and hyphen joins). A
    hyphen pair is forced into the last window that contains BOTH members
    so reconciliation never spans a target boundary.
    """
    membership: dict[str, list[int]] = {}
    for i, w in enumerate(windows):
        for lid in w:
            membership.setdefault(lid, []).append(i)

    target_win: dict[str, int] = {lid: idxs[-1] for lid, idxs in membership.items()}

    # Hyphen atomicity (audit P0): a chain of 3+ lines (PART1→BOTH→…→PART2)
    # must be targeted in ONE window. The previous pairwise pin used
    # last-write-wins, so on a 3-line chain the middle line got re-pinned
    # by its forward partner AFTER its backward partner, splitting the pair
    # across two chunks. Pin whole transitively-connected COMPONENTS
    # instead (union-find over every hyphen link, the pattern _try_block
    # already uses), assigning each component the LAST window common to
    # ALL its members — order-independent by construction.
    parent: dict[str, str] = {lid: lid for lid in membership}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for lid in list(membership):
        lm = line_by_id.get(lid)
        if lm is None:
            continue
        for partner in (
            getattr(lm, "hyphen_pair_line_id", None),
            getattr(lm, "hyphen_forward_pair_id", None),
        ):
            if partner and partner in membership:
                union(lid, partner)

    components: dict[str, list[str]] = {}
    for lid in membership:
        components.setdefault(find(lid), []).append(lid)

    for members in components.values():
        if len(members) < 2:
            continue
        # Intersection of every member's membership set = windows that
        # contain the WHOLE component. Target the last such window.
        common: set[int] = set(membership[members[0]])
        for m in members[1:]:
            common &= set(membership[m])
        if common:
            win = max(common)
            for m in members:
                target_win[m] = win
        # If no single window holds the whole component (a chain longer
        # than any window — the planner caps chains to a window, so this
        # is the pathological over-cap case), leave the per-line last-window
        # assignment: the LINE-granularity downgrade + unlink handles it.

    return [
        [lid for lid in w if target_win.get(lid) == i] for i, w in enumerate(windows)
    ]


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
        partner_id = forward_partner_id(lm)
        if partner_id:
            pair = line_by_id.get(partner_id)
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
        return ChunkPlan(
            page_id=page.page_id, chunks=[], granularity=ChunkGranularity.WINDOW
        )

    window_size = config.line_window_size
    overlap = config.line_window_overlap

    window_line_ids: list[list[str]] = []
    start = 0

    while start < n:
        # A window is bounded by BOTH the line count and the char
        # budget (historically only PAGE/BLOCK honoured the char budget;
        # a window of pathologically long lines blew straight past
        # max_input_chars_per_request). At least one line always enters,
        # even over budget — a single line is atomic.
        end = start + 1
        chars = len(lines[start].ocr_text)
        while end < min(start + window_size, n):
            c = len(lines[end].ocr_text)
            if chars + c > config.max_input_chars_per_request:
                break
            chars += c
            end += 1
        core_end = end  # budget/size-limited end, drives the overlap step

        # Extend to keep hyphen chains intact at window boundary,
        # but cap to avoid unbounded growth beyond the line budget.
        # Chain atomicity deliberately outranks the char budget: splitting
        # a hyphenated word across chunks corrupts reconciliation, while
        # a temporarily oversized request only risks a producer error
        # (retried / downgraded). The extension stays line-capped.
        extension_limit = max(config.max_lines_per_request, end - start + 10)
        max_end = min(n, start + extension_limit)
        while end < max_end:
            last_in_window = lines[end - 1]
            next_line = lines[end]
            if should_stay_in_same_chunk(last_in_window, next_line):
                end += 1
            else:
                break

        window_line_ids.append([lines[i].line_id for i in range(start, end)])

        # Step relative to the ACTUAL core window when the char budget
        # shortened it, so nothing is skipped (a fixed step would jump
        # past unvisited lines). When the budget did not bind, keep the
        # historical fixed step exactly (byte-parity with the fixed-step
        # planner, including its tail-window behaviour near page end).
        budget_bound = core_end < min(start + window_size, n)
        if budget_bound:
            next_start = max(start + 1, core_end - overlap)
        else:
            next_start = start + (window_size - overlap)
        # Defensive progress guard: the config validator forbids
        # overlap >= window_size, but pydantic's model_copy(update=...)
        # BYPASSES validation — without this clamp such a config spins
        # this loop forever (review finding, reproduced).
        if next_start <= start:
            next_start = start + 1
        start = next_start

    # F8 — each line is a target in exactly one window (its last, best-context
    # window); overlaps become pure context in the other window.
    line_by_id = {lm.line_id: lm for lm in lines}
    targets_per_window = _assign_window_targets(window_line_ids, line_by_id)

    chunks = [
        _make_chunk(
            document_id,
            page.page_id,
            ChunkGranularity.WINDOW,
            ids,
            target_line_ids=targets,
        )
        for ids, targets in zip(window_line_ids, targets_per_window)
    ]

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
        # The chain follow is capped: an adversarial page where
        # every line ends in a dash would otherwise produce one unbounded
        # request.
        while j < len(lines) and len(chain_ids) < config.max_lines_per_request:
            cur = lines[j]
            forward_pair = forward_partner_id(cur)
            if (
                forward_pair
                and j + 1 < len(lines)
                and lines[j + 1].line_id == forward_pair
            ):
                chain_ids.append(lines[j + 1].line_id)
                j += 1
            else:
                break

        # Pair atomicity (core invariant): if the cap cut the chain
        # BETWEEN a forward line and its partner, the two halves would sit
        # in different chunks as a still-linked pair — the validator skips
        # such pairs (both members must be in-chunk) and the reconciler
        # could write across the boundary. UNLINK the cut pair explicitly:
        # both sides degrade to independent lines (their OCR text,
        # including the trailing dash, is preserved verbatim — the
        # conservative fallback), so every remaining pair is fully
        # contained in one chunk and atomicity stays true by construction.
        if len(chain_ids) >= config.max_lines_per_request and j + 1 < len(lines):
            tail = lines[j]
            head = lines[j + 1]
            if forward_partner_id(tail) == head.line_id:
                if tail.hyphen_role == HyphenRole.BOTH:
                    tail.hyphen_role = HyphenRole.PART2  # keeps backward link
                    tail.hyphen_forward_pair_id = None
                    tail.hyphen_forward_pair_page_id = None
                    tail.hyphen_forward_subs_content = None
                else:  # PART1
                    tail.hyphen_role = HyphenRole.NONE
                    tail.hyphen_pair_line_id = None
                    tail.hyphen_pair_page_id = None
                    tail.hyphen_subs_content = None
                if head.hyphen_role == HyphenRole.BOTH:
                    # Keeps its own forward pair; loses the backward link.
                    # PART1 carries its forward link/subs in the plain pair
                    # fields, so migrate them from the BOTH forward fields.
                    head.hyphen_role = HyphenRole.PART1
                    head.hyphen_pair_line_id = head.hyphen_forward_pair_id
                    head.hyphen_pair_page_id = head.hyphen_forward_pair_page_id
                    head.hyphen_subs_content = head.hyphen_forward_subs_content
                    head.hyphen_source_explicit = head.hyphen_forward_explicit
                    head.hyphen_forward_pair_id = None
                    head.hyphen_forward_pair_page_id = None
                    head.hyphen_forward_subs_content = None
                else:  # PART2
                    head.hyphen_role = HyphenRole.NONE
                    head.hyphen_pair_line_id = None
                    head.hyphen_pair_page_id = None
                    head.hyphen_subs_content = None

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


# --- public surface ---
__all__ = [
    "downgrade_granularity",
    "plan_page",
]
