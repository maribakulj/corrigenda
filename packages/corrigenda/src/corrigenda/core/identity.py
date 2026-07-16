"""Identity-uniqueness validation shared by format parsers and pipeline (ADR-007).

Every association between a correction and its physical line — the
rewriters' ``line_by_id`` lookup, the trace projection, hyphen partner
resolution — is keyed by ``line_id`` *within one source file*. ``page_id``
must also be unique per file (cross-page hyphen linking, trace keys) and
``block_id`` unique *within its page* (block-granularity planning and
every other block lookup is page-scoped — per-page OCR tools that reuse
``block_0``/``block_1`` on every page of a file are legitimate). A file
that repeats an ID within the relevant scope is structurally ambiguous:
silently continuing would risk applying a correction to the wrong
physical line, so the library refuses it up front with
:class:`~corrigenda.errors.DuplicateIdError`.

Scope note: line/page uniqueness is required **per source file**, not
globally. Two *different* files may legitimately reuse the same
``line_id`` — every downstream lookup is already scoped to a single
file's pages. Cross-file ``page_id`` collisions, however, corrupt
document-wide lookups (trace keys, per-page image/dimension maps) and are
refused by :func:`ensure_unique_page_ids_across_files` — the format
builders' ``disambiguate_page_ids`` qualifies them automatically.

Pure core: no lxml, no format import — the import-contract test keeps it so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from corrigenda.core.schemas import LineManifest, PageManifest
from corrigenda.errors import DuplicateIdError, ParseError

_MAX_REPORTED = 5


@dataclass(frozen=True, slots=True)
class LineRef:
    """Fully qualified line identity — the ONLY key type for document-wide
    line lookups (ADR-009).

    ``page_id`` is document-unique (ADR-007: the format builders
    disambiguate cross-file collisions and the pipeline door refuses
    duplicates), so the pair qualifies a physical line unambiguously
    across every source file of a run. The source file is deliberately
    NOT part of the key: it is a property of the page
    (``PageManifest.source_file``), and adding it would allow two keys
    for one physical line. Bare ``line_id`` strings remain legal only
    for lookups already scoped to ONE page or ONE source file.
    """

    page_id: str
    line_id: str


def line_ref(lm: LineManifest) -> LineRef:
    """The :class:`LineRef` of a manifest line."""
    return LineRef(page_id=lm.page_id, line_id=lm.line_id)


def _format_duplicates(kind: str, counts: Counter[str]) -> str | None:
    dups = [(i, n) for i, n in counts.items() if n > 1]
    if not dups:
        return None
    shown = ", ".join(f"{i!r} ({n}×)" for i, n in dups[:_MAX_REPORTED])
    more = f" and {len(dups) - _MAX_REPORTED} more" if len(dups) > _MAX_REPORTED else ""
    return f"{kind}: {shown}{more}"


def _raise_if_duplicates(source_name: str, problems: list[str | None]) -> None:
    real = [p for p in problems if p]
    if real:
        raise DuplicateIdError(
            f"duplicate identities in {source_name!r} — {'; '.join(real)}. "
            "IDs must be unique within their scope: correction-to-line "
            "association would be ambiguous (ADR-007). Fix the document's IDs "
            "and retry."
        )


def ensure_unique_identities(pages: Iterable[PageManifest], source_name: str) -> None:
    """Raise :class:`DuplicateIdError` if ``pages`` (one source file's pages)
    repeat a page id, a block id within a page, or a line id.

    Called by both format parsers right after building a file's manifests,
    by the pipeline on its input manifest (defence in depth for hand-built
    manifests that never went through a parser), and by both rewriters.
    """
    page_ids: Counter[str] = Counter()
    line_ids: Counter[str] = Counter()
    block_problems: list[str | None] = []

    for page in pages:
        page_ids[page.page_id] += 1
        # Block lookups are page-scoped everywhere downstream — validate
        # per page so per-page OCR exports reusing block_0/block_1 on
        # every page keep parsing (a per-file scope would refuse
        # legitimate documents).
        block_ids: Counter[str] = Counter(b.block_id for b in page.blocks)
        block_problems.append(
            _format_duplicates(f"block ID(s) on page {page.page_id!r}", block_ids)
        )
        for lm in page.lines:
            line_ids[lm.line_id] += 1

    _raise_if_duplicates(
        source_name,
        [
            _format_duplicates("Page ID(s)", page_ids),
            *block_problems,
            _format_duplicates("line ID(s)", line_ids),
        ],
    )


def ensure_unique_element_ids(
    raw_ids: Iterable[str | None], source_name: str, *, kind: str
) -> None:
    """Raise :class:`DuplicateIdError` on repeated non-empty ids in
    ``raw_ids`` (``None``/empty entries — elements without an id — are
    ignored: they can never be matched by an id lookup).

    Used by the format parsers to scan the WHOLE document tree for
    duplicate ``TextLine`` ids: the rewriters match elements over the full
    tree (margins included), so the parse-time gate must cover the same
    scope or a duplicate would only surface at rewrite time, after the
    full producer spend.
    """
    counts: Counter[str] = Counter(i for i in raw_ids if i)
    _raise_if_duplicates(source_name, [_format_duplicates(kind, counts)])


def ensure_element_ids_present(
    raw_ids: Iterable[str | None], source_name: str, *, kind: str
) -> None:
    """Raise :class:`ParseError` when any element lacks an id.

    An id-less element cannot round-trip: the rewriters match on the real
    ``id``/``ID`` attribute, so a parser-fabricated placeholder id would be
    unmatchable and that element's correction silently dropped (the LLM
    spend wasted, the line left as raw OCR while the manifest reports it
    corrected). Valid ALTO/PAGE requires ids on ``TextLine`` elements, so
    the library refuses non-conformant input up front rather than losing
    corrections — mirroring the duplicate-id gate above.
    """
    missing = sum(1 for i in raw_ids if not i)
    if missing:
        raise ParseError(
            f"{source_name!r} contains {missing} {kind} without an id — "
            "an id-less line cannot round-trip through the rewriter (its "
            "correction would be silently dropped). Assign ids and retry."
        )


def ensure_unique_page_ids_across_files(pages: Iterable[PageManifest]) -> None:
    """Raise :class:`DuplicateIdError` when the same ``page_id`` appears in
    two different source files of one document.

    Trace keys and the pipeline's per-page image/dimension lookups are
    document-wide, so cross-file page_id collisions corrupt them. The
    format builders' ``disambiguate_page_ids`` qualifies collisions
    automatically; this guard protects hand-built manifests.
    """
    seen: dict[str, str] = {}
    for page in pages:
        first = seen.setdefault(page.page_id, page.source_file)
        if first != page.source_file:
            raise DuplicateIdError(
                f"page_id {page.page_id!r} appears in both {first!r} and "
                f"{page.source_file!r} — cross-file page ids must be "
                "disambiguated before running (the format parsers' "
                "build_document_manifest does this automatically)."
            )


__all__ = [
    "LineRef",
    "line_ref",
    "ensure_unique_identities",
    "ensure_unique_element_ids",
    "ensure_element_ids_present",
    "ensure_unique_page_ids_across_files",
]
