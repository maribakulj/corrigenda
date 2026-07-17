"""Atomic correction units — hyphen groups (ADR-010, first slice).

A hyphenated word split across lines is ONE thing to correct: its member
lines must travel together through planning, validation, reconciliation
and reverts. The engine historically re-derived that grouping ad hoc at
every site — union-finds in the planner, transitive revert worklists in
the pipeline — from the per-line pointer fields
(``hyphen_pair_line_id`` / ``hyphen_forward_pair_id``). Each ad-hoc
derivation is an opportunity to disagree with the others.

This module is the single derivation: maximal hyphen-linked components,
members in reading order, keyed by :class:`~corrigenda.core.identity.LineRef`.
A simple pair is a two-member group; a PART1→BOTH→PART2 chain is one
three-member group; a cross-page pair is one group whose members live on
two pages (``spans_pages``). Consumers that are page-scoped (the chunk
planner — cross-page joins are reconciled, never planned together) work
on the page projection of a group.

Pure core: no lxml, no formats import (import-contract test).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from corrigenda.core.identity import LineRef, line_ref
from corrigenda.core.pairing import forward_partner_id
from corrigenda.core.schemas import HyphenRole, HyphenSplit, LineManifest


@dataclass(frozen=True)
class HyphenGroup:
    """A maximal chain of hyphen-linked lines, in reading order."""

    #: Member lines ordered by document reading order.
    members: tuple[LineRef, ...]
    #: True when the members do not all live on one page (cross-page
    #: joins are reconciled with cross-page context, never planned into
    #: one chunk).
    spans_pages: bool
    #: True when EVERY link in the group is source-explicit (SUBS_TYPE
    #: in the document). Conservative heuristic mode hangs off this: a
    #: heuristic group never invents SUBS_CONTENT.
    explicit: bool

    def member_ids_on_page(self, page_id: str) -> tuple[str, ...]:
        """The group's bare line ids on ONE page (page-scoped consumers)."""
        return tuple(m.line_id for m in self.members if m.page_id == page_id)


def derive_hyphen_groups(lines: Iterable[LineManifest]) -> tuple[HyphenGroup, ...]:
    """Maximal hyphen components over ``lines``, in reading order.

    Links pointing at lines outside ``lines`` are ignored (a dangling
    partner is a reconcile-time concern, not a grouping one), so the
    same function derives document-wide groups (pass every page's lines)
    and page-local ones (pass one page's lines — a cross-page pair then
    simply contributes its on-page member as a singleton, which is
    dropped like any other ungrouped line).
    """
    by_ref: dict[LineRef, LineManifest] = {}
    order: dict[LineRef, int] = {}
    for lm in lines:
        ref = line_ref(lm)
        by_ref[ref] = lm
        order[ref] = lm.line_order_global

    parent: dict[LineRef, LineRef] = {ref: ref for ref in by_ref}

    def find(x: LineRef) -> LineRef:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: LineRef, b: LineRef) -> None:
        parent[find(a)] = find(b)

    def partner_ref(
        lm: LineManifest, pid: str | None, ppage: str | None
    ) -> LineRef | None:
        if not pid:
            return None
        return LineRef(page_id=ppage or lm.page_id, line_id=pid)

    for ref, lm in by_ref.items():
        for partner in (
            partner_ref(lm, lm.hyphen_pair_line_id, lm.hyphen_pair_page_id),
            partner_ref(lm, lm.hyphen_forward_pair_id, lm.hyphen_forward_pair_page_id),
        ):
            if partner is not None and partner in parent:
                union(ref, partner)

    components: dict[LineRef, list[LineRef]] = {}
    for ref in by_ref:
        components.setdefault(find(ref), []).append(ref)

    groups: list[HyphenGroup] = []
    for members in components.values():
        if len(members) < 2:
            continue  # an unlinked line is not a hyphen group
        ordered = tuple(sorted(members, key=lambda r: order[r]))
        explicit = all(_links_explicit(by_ref[r]) for r in ordered)
        groups.append(
            HyphenGroup(
                members=ordered,
                spans_pages=len({r.page_id for r in ordered}) > 1,
                explicit=explicit,
            )
        )
    groups.sort(key=lambda g: order[g.members[0]])
    return tuple(groups)


def _links_explicit(lm: LineManifest) -> bool:
    """Every link THIS line participates in is source-explicit."""
    if lm.hyphen_pair_line_id and not lm.hyphen_source_explicit:
        return False
    if lm.hyphen_forward_pair_id and not lm.hyphen_forward_explicit:
        return False
    return True


def hyphen_group_by_line(
    groups: Iterable[HyphenGroup],
) -> dict[LineRef, HyphenGroup]:
    """Index: every member line → its (unique) group."""
    index: dict[LineRef, HyphenGroup] = {}
    for group in groups:
        for member in group.members:
            index[member] = group
    return index


def split_forward_link(tail: LineManifest, head: LineManifest) -> HyphenSplit:
    """Sever the forward hyphen link ``tail`` → ``head`` — THE unit
    SPLIT operation (ADR-010).

    The LINE planner calls this when a chain exceeds
    ``max_lines_per_request``: leaving the pair straddling two chunks
    would break pair atomicity (the validator skips pairs that are not
    fully in-chunk and the reconciler could write across the boundary),
    so the cut pair degrades to two independent lines. Both sides keep
    their OCR text verbatim, trailing dash included — the conservative
    fallback. Only roles and link fields change:

    * ``tail`` BOTH → PART2 (keeps its backward link), PART1 → NONE;
    * ``head`` BOTH → PART1 (its own forward link/subs migrate into the
      plain pair fields, where PART1 carries them), PART2 → NONE.

    The pointer fields remain the storage of record until a later slice
    makes groups authoritative; until then this function is the only
    place that severs a link, and the returned record — carried on the
    :class:`~corrigenda.core.schemas.ChunkPlan` — is how a consumer
    learns the cut happened at all.
    """
    if forward_partner_id(tail) != head.line_id:
        raise RuntimeError(
            f"split_forward_link: {tail.line_id!r} does not continue onto "
            f"{head.line_id!r} — refusing to sever a link that is not there "
            "(engine bug)"
        )
    if tail.hyphen_role == HyphenRole.BOTH:
        tail.hyphen_role = HyphenRole.PART2
        tail.hyphen_forward_pair_id = None
        tail.hyphen_forward_pair_page_id = None
        tail.hyphen_forward_subs_content = None
    else:  # PART1
        tail.hyphen_role = HyphenRole.NONE
        tail.hyphen_pair_line_id = None
        tail.hyphen_pair_page_id = None
        tail.hyphen_subs_content = None
    if head.hyphen_role == HyphenRole.BOTH:
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
    return HyphenSplit(
        page_id=tail.page_id,
        tail_line_id=tail.line_id,
        head_line_id=head.line_id,
    )


__all__ = [
    "HyphenGroup",
    "derive_hyphen_groups",
    "hyphen_group_by_line",
    "split_forward_link",
]
