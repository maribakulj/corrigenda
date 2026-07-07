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

    Structural groups are re-emitted verbatim (their captured ``name {body}``
    span, single-space joined). When every group is offset-anchored the
    result is the empty string — the caller then removes the attribute
    entirely rather than leaving ``custom=""``.
    """
    kept: list[str] = []
    removed = 0
    for match in _GROUP_RE.finditer(custom):
        name, body = match.group(1), match.group(2)
        if _body_has_offset(body):
            removed += 1
        else:
            kept.append(f"{name} {{{body}}}")
    return " ".join(kept), removed


__all__ = ["strip_offset_groups"]
