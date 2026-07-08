"""Format-agnostic hyphen-pair linking (§6.3 parity).

Once a parser has set each line's ``hyphen_role`` (from ALTO's explicit
``SUBS_TYPE``/``HYP`` markup or PAGE's terminal-character heuristic), the
*second pass* that links PART1/BOTH lines to their forward partners is
identical across formats: it reasons purely about ``LineManifest`` roles
and a ``PairingPolicy``, never about XML. It lived in
``formats/alto/parser.py`` until PAGE support needed the exact same
behaviour; hoisting it into the pure core keeps the two format parsers
from drifting and makes ``§6.3`` ("same DocumentManifest regardless of
format") true by construction rather than by discipline.

Nothing here imports lxml or any format module — the import-contract test
keeps it that way.
"""

from __future__ import annotations

from corrigenda.core.schemas import (
    DEFAULT_PAIRING_POLICY,
    HyphenRole,
    LineManifest,
    PageManifest,
    PairingPolicy,
)

#: Terminal characters a heuristic parser treats as a word-break hyphen.
#: ALTO relies on explicit ``SUBS_TYPE``/``HYP`` markup and only falls back
#: to a plain ``-``; PAGE has no such markup, so it scans for the wider
#: Transkribus/Fraktur repertoire (P5): hyphen-minus, the ``¬`` negation
#: sign Transkribus emits, the ``⸗`` double oblique of Fraktur, and the
#: U+00AD soft hyphen.
HYPHEN_CHARS: tuple[str, ...] = ("-", "¬", "⸗", "­")


def trailing_hyphen_char(text: str, hyphen_chars: tuple[str, ...]) -> str | None:
    """Return the trailing hyphen character of ``text``, or ``None``.

    A genuine word-break hyphen is signalled by the last non-space token
    ending in one of ``hyphen_chars`` **with an alphabetic character
    immediately before it** — the same narrowing the ALTO heuristic uses
    to reject year ranges (``1789-``) and list markers (``n°5-``). Returns
    the matched hyphen character (so the caller can preserve it verbatim,
    per P5) or ``None`` when no word-break hyphen is present.
    """
    tokens = text.split()
    if not tokens:
        return None
    last = tokens[-1]
    for ch in hyphen_chars:
        if last.endswith(ch):
            bare = last[: -len(ch)]
            if bare and bare[-1].isalpha():
                return ch
    return None


def forward_partner_id(lm: LineManifest) -> str | None:
    """The line_id this line's word continues ONTO, if any.

    A ``PART1`` line continues to its ``hyphen_pair_line_id``; a ``BOTH``
    line (tail of one hyphenated word, head of the next) continues to its
    ``hyphen_forward_pair_id``; ``PART2`` / ``NONE`` continue nowhere.

    Single source of truth for "who is my forward hyphen partner?": the
    LINE-chain planner, the cross-block union-find, and the same-chunk
    predicate all resolve the forward link through here, so the
    role→field mapping (which field holds the forward id per role) lives in
    exactly one place instead of being re-encoded at each call site.

    NB this is the strictly *forward* partner. The window-target assignment
    keeps a chain atomic in either direction and uses its own broader
    ``planner._hyphen_partner_id`` (backward-inclusive); the two notions are
    deliberately distinct.
    """
    if lm.hyphen_role == HyphenRole.PART1:
        return lm.hyphen_pair_line_id
    if lm.hyphen_role == HyphenRole.BOTH:
        return lm.hyphen_forward_pair_id
    return None


def link_hyphen_pairs(
    lines: list[LineManifest],
    pairing_policy: PairingPolicy = DEFAULT_PAIRING_POLICY,
) -> None:
    """
    Second pass: link PART1/BOTH lines to their forward partners.

    A line with role PART1 or BOTH has a forward PART1 relationship.
    The next line is linked as PART2/BOTH (backward side).

    For PART1:  pair_line_id = forward partner, subs_content = pair subs
    For BOTH:   forward_pair_id = forward partner, forward_subs_content = pair subs
                (backward fields were already set by a previous iteration)

    ``pairing_policy`` (F7) gates each candidate: when it rejects a pair
    (e.g. the candidate sits too far below, or in an unrelated block), the
    link is skipped and the PART1 line is left unpaired for the downstream
    guards to handle. The default policy accepts every next line — the
    historical purely-sequential behaviour.
    """
    for i, line in enumerate(lines):
        # Skip lines that don't have a forward (PART1) role
        if line.hyphen_role not in (HyphenRole.PART1, HyphenRole.BOTH):
            continue
        if i + 1 >= len(lines):
            continue

        candidate = lines[i + 1]

        # Accept PART2, BOTH, or NONE as forward partner
        if candidate.hyphen_role not in (
            HyphenRole.PART2,
            HyphenRole.BOTH,
            HyphenRole.NONE,
        ):
            continue

        # F7 — injectable pairing seam. Default policy always accepts.
        if not pairing_policy.can_pair(line, candidate):
            continue

        # Mark NONE candidate as PART2
        if candidate.hyphen_role == HyphenRole.NONE:
            if line.hyphen_role == HyphenRole.BOTH:
                candidate.hyphen_role = HyphenRole.PART2
                candidate.hyphen_source_explicit = line.hyphen_forward_explicit
            else:
                candidate.hyphen_role = HyphenRole.PART2
                candidate.hyphen_source_explicit = line.hyphen_source_explicit

        # Determine subs_content and set links for this pair
        if line.hyphen_role == HyphenRole.BOTH:
            # Forward side of a BOTH line
            subs = (
                line.hyphen_forward_subs_content
                or candidate.hyphen_subs_content
                or None
            )

            # Set forward link on the BOTH line
            line.hyphen_forward_pair_id = candidate.line_id
            line.hyphen_forward_pair_page_id = candidate.page_id
            if subs:
                line.hyphen_forward_subs_content = subs

            # Set backward link on the candidate
            candidate.hyphen_pair_line_id = line.line_id
            candidate.hyphen_pair_page_id = line.page_id
            if subs:
                candidate.hyphen_subs_content = subs
        else:
            # Regular PART1 line
            subs = line.hyphen_subs_content or candidate.hyphen_subs_content

            # Bidirectional link (page_id qualifies for cross-page disambiguation)
            line.hyphen_pair_line_id = candidate.line_id
            line.hyphen_pair_page_id = candidate.page_id
            candidate.hyphen_pair_line_id = line.line_id
            candidate.hyphen_pair_page_id = line.page_id

            if subs:
                line.hyphen_subs_content = subs
                candidate.hyphen_subs_content = subs


def disambiguate_page_ids(
    parsed: list[tuple[str, list[PageManifest]]],
) -> None:
    """Prefix colliding Page IDs with their source filename.

    Multiple transcription files commonly declare the same Page ID
    (``"Page1"``, ``"P1"``…) — a per-scan workflow practically guarantees
    this. Without disambiguation, the pipeline's cross-page hyphen partner
    lookup picks the wrong page, intra-page hyphen pair_page_id refs become
    ambiguous, and the trace/diff/layout endpoints emit duplicate page_id
    values to the frontend.

    This is called BEFORE cross-page hyphen linking so that the qualified
    IDs flow into ``hyphen_pair_page_id`` naturally.
    """
    counts: dict[str, int] = {}
    for _, pages in parsed:
        for p in pages:
            counts[p.page_id] = counts.get(p.page_id, 0) + 1

    colliding = {pid for pid, n in counts.items() if n > 1}
    if not colliding:
        return

    for source_name, pages in parsed:
        for p in pages:
            old_pid = p.page_id
            if old_pid not in colliding:
                continue
            new_pid = f"{source_name}::{old_pid}"
            p.page_id = new_pid
            for b in p.blocks:
                b.page_id = new_pid
            for lm in p.lines:
                lm.page_id = new_pid
                # Intra-page hyphen partner refs were set to the old page_id
                # by link_hyphen_pairs during parsing. Rewrite them to the
                # qualified id so downstream lookups stay consistent.
                if lm.hyphen_pair_page_id == old_pid:
                    lm.hyphen_pair_page_id = new_pid
                if lm.hyphen_forward_pair_page_id == old_pid:
                    lm.hyphen_forward_pair_page_id = new_pid


def link_cross_page_hyphens(
    all_pages: list[PageManifest],
    pairing_policy: PairingPolicy = DEFAULT_PAIRING_POLICY,
) -> None:
    """Link a PART1/BOTH line at the bottom of page N to the top of page N+1.

    ``link_hyphen_pairs`` works on any list of consecutive lines, so a
    2-element ``[last_line, first_line]`` list is enough. Only fires when
    the last line looks like an unlinked PART1/BOTH.
    """
    for i in range(len(all_pages) - 1):
        if not all_pages[i].lines or not all_pages[i + 1].lines:
            continue
        last_line = all_pages[i].lines[-1]
        first_line = all_pages[i + 1].lines[0]
        needs_forward_link = (
            last_line.hyphen_role == HyphenRole.PART1
            and not last_line.hyphen_pair_line_id
        ) or (
            last_line.hyphen_role == HyphenRole.BOTH
            and not last_line.hyphen_forward_pair_id
        )
        if needs_forward_link:
            link_hyphen_pairs([last_line, first_line], pairing_policy)


__all__ = [
    "HYPHEN_CHARS",
    "trailing_hyphen_char",
    "forward_partner_id",
    "link_hyphen_pairs",
    "disambiguate_page_ids",
    "link_cross_page_hyphens",
]
