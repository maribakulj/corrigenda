"""Identity-uniqueness validation shared by format parsers and pipeline (P0-5).

Every association between a correction and its physical line — the
rewriters' ``line_by_id`` lookup, the trace projection, hyphen partner
resolution — is keyed by ``line_id`` *within one source file*. The same
holds for ``page_id`` (cross-page hyphen linking, trace keys) and
``block_id`` (block-granularity planning). A file where two elements share
an ID is structurally ambiguous: silently continuing would risk applying a
correction to the wrong physical line, so the library refuses it up front
with :class:`~corrigenda.errors.DuplicateIdError`.

Scope note: uniqueness is required **per source file**, not globally. Two
*different* files may legitimately reuse the same ``line_id`` — every
downstream lookup is already scoped to a single file's pages.

Pure core: no lxml, no format import — the import-contract test keeps it so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from corrigenda.core.schemas import PageManifest
from corrigenda.errors import DuplicateIdError

_MAX_REPORTED = 5


def _format_duplicates(kind: str, counts: Counter[str]) -> str | None:
    dups = [(i, n) for i, n in counts.items() if n > 1]
    if not dups:
        return None
    shown = ", ".join(f"{i!r} ({n}×)" for i, n in dups[:_MAX_REPORTED])
    more = f" and {len(dups) - _MAX_REPORTED} more" if len(dups) > _MAX_REPORTED else ""
    return f"{kind}: {shown}{more}"


def ensure_unique_identities(pages: Iterable[PageManifest], source_name: str) -> None:
    """Raise :class:`DuplicateIdError` if ``pages`` (one source file's pages)
    repeat a page, block or line identity.

    Called by both format parsers right after building a file's manifests,
    and by the pipeline on its input manifest (defence in depth for
    hand-built manifests that never went through a parser).
    """
    page_ids: Counter[str] = Counter()
    block_ids: Counter[str] = Counter()
    line_ids: Counter[str] = Counter()

    for page in pages:
        page_ids[page.page_id] += 1
        for block in page.blocks:
            block_ids[block.block_id] += 1
        for lm in page.lines:
            line_ids[lm.line_id] += 1

    problems = [
        msg
        for msg in (
            _format_duplicates("Page ID(s)", page_ids),
            _format_duplicates("block ID(s)", block_ids),
            _format_duplicates("line ID(s)", line_ids),
        )
        if msg
    ]
    if problems:
        raise DuplicateIdError(
            f"duplicate identities in {source_name!r} — {'; '.join(problems)}. "
            "IDs must be unique within a source file: correction-to-line "
            "association would be ambiguous (P0-5). Fix the document's IDs "
            "and retry."
        )


__all__ = ["ensure_unique_identities"]
