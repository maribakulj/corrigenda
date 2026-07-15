"""PAGE ``custom`` microformat handling (spec 6.2 P6).

Transkribus packs annotations into the ``custom`` attribute of ``TextLine``
and ``Word`` elements, e.g.::

    custom="readingOrder {index:0;} textStyle {offset:12; length:5;} …"

Groups come in two kinds:

  - **structural** (``readingOrder``, ``structure``) — no character offsets;
    they survive a text edit unchanged and are preserved verbatim.
  - **offset-anchored** (``textStyle``, semantic tags with
    ``offset``/``length``) — they point at character ranges in the OLD line
    text. Once the text changes those offsets are stale, so the whole group
    is dropped (the "never keep invalidated data" doctrine, cf. ALTO F2)
    and counted. A future span-only edit path (v2.x) can instead remap the
    offsets through the EditScript; that is out of scope here.
"""

from __future__ import annotations

import re

# A group is ``name {body}`` with the body a run of ``key:value;`` pairs.
_GROUP_RE = re.compile(r"(\w+)\s*\{([^}]*)\}")

_OFFSET_KEYS = frozenset({"offset", "length"})


def _body_has_offset(body: str) -> bool:
    for pair in body.split(";"):
        key = pair.split(":", 1)[0].strip()
        if key in _OFFSET_KEYS:
            return True
    return False


def strip_offset_groups(custom: str) -> tuple[str, int]:
    """Return ``(new_custom, removed_count)`` with offset-anchored groups gone.

    Structural groups are preserved VERBATIM: each kept group
    is the exact source slice (``match.group(0)``), and the original text
    BETWEEN two kept groups survives untouched when no removed group sat
    there (non-Transkribus exporters legitimately write
    ``readingOrder{index:0;}`` with no space — reconstruction from the
    captured name/body silently normalised it). Where a removed group
    separated two kept ones, a single space joins them. When nothing is
    removed the input is returned byte-identical. When every group is
    offset-anchored the result is the empty string — the caller then
    removes the attribute entirely rather than leaving ``custom=""``.
    """
    kept_spans: list[tuple[int, int]] = []
    removed = 0
    for match in _GROUP_RE.finditer(custom):
        if _body_has_offset(match.group(2)):
            removed += 1
        else:
            kept_spans.append(match.span())

    if removed == 0:
        return custom, 0

    parts: list[str] = []
    prev_end: int | None = None
    for start, end in kept_spans:
        if prev_end is not None:
            between = custom[prev_end:start]
            # Verbatim separator only if no removed group sat in it —
            # a group brace in the gap means something was cut out.
            parts.append(between if "{" not in between else " ")
        parts.append(custom[start:end])
        prev_end = end
    return "".join(parts), removed


__all__ = ["strip_offset_groups"]
