"""PAGE-specific namespace + geometry helpers.

The format-agnostic pieces — namespace detection, tag qualification, and
the hardened ``make_safe_parser`` — live in :mod:`corrigenda.formats._xml`
and are re-exported here under the same private names PAGE call sites
already use. PAGE no longer reaches sideways into ``formats.alto`` for the
shared parser (the former ``page → alto`` edge is gone); both formats are
siblings of the neutral ``_xml`` module. Only the PAGE-specific schema-year
/ ``MetadataItem`` logic and the polygon→bbox conversion stay here.
"""

from __future__ import annotations

from corrigenda.core._parse import parse_int_tolerant
from corrigenda.formats._xml import (
    detect_namespace as _detect_namespace,
    make_safe_parser as make_safe_parser,
    tag as _tag,
)

#: PAGE namespace URIs are dated; anything from this year onward carries
#: the richer ``Metadata/MetadataItem`` provenance slot (P7). Earlier
#: schemas fall back to ``Metadata/Comments``.
_METADATA_ITEM_MIN_YEAR = 2019


def _namespace_year(ns: str) -> int | None:
    """Extract the schema year from a PAGE namespace URI.

    ``…/pagecontent/2019-07-15`` → ``2019``. Returns ``None`` when the URI
    carries no recognisable ``/YYYY-MM-DD`` date (custom or malformed ns).
    """
    tail = ns.rstrip("/").rsplit("/", 1)[-1]
    head = tail.split("-", 1)[0]
    if len(head) == 4 and head.isdigit():
        return int(head)
    return None


def supports_metadata_item(ns: str) -> bool:
    """True if the schema year is new enough for ``MetadataItem`` (P7)."""
    year = _namespace_year(ns)
    return year is not None and year >= _METADATA_ITEM_MIN_YEAR


def polygon_to_bbox(points: str) -> tuple[int, int, int, int]:
    """Convert a PAGE ``Coords@points`` polygon to an (hpos, vpos, w, h) bbox.

    ``points`` is a space-separated list of ``x,y`` pairs
    (``"617,1046 3450,1046 3450,5797 617,5797"``). Returns the enclosing
    axis-aligned box the planner needs (P1). Tolerates float coordinates
    (truncated toward zero, matching the ALTO ``_int_attr`` policy) and
    skips malformed pairs rather than aborting the whole file. An empty or
    unparseable polygon yields a zero box.
    """
    xs: list[int] = []
    ys: list[int] = []
    for pair in points.split():
        if "," not in pair:
            continue
        x_str, _, y_str = pair.partition(",")
        # An empty side (",5") must skip the pair, not default to 0 —
        # keep the pre-helper semantics before delegating to
        # parse_int_tolerant (whose empty-string behaviour is `default`).
        if not x_str or not y_str:
            continue
        try:
            # Parse BOTH coordinates before mutating either list, so a pair
            # with a good x but a bad y (heritage-OCR garbage) is skipped
            # atomically. Appending x first left a half-added pair (xs longer
            # than ys), inflating the bbox with a coordinate the docstring
            # promises to skip. Audit-F9 — the shared strict parser also
            # surfaces inf/overflow-shaped coordinates as ValueError
            # (pre-fix ``int(float("inf"))`` escaped as OverflowError and
            # aborted the whole file, violating the skip promise above).
            xi = parse_int_tolerant(x_str, strict=True)
            yi = parse_int_tolerant(y_str, strict=True)
        except ValueError:
            continue
        xs.append(xi)
        ys.append(yi)
    if not xs or not ys:
        return 0, 0, 0, 0
    hpos, vpos = min(xs), min(ys)
    return hpos, vpos, max(xs) - hpos, max(ys) - vpos


__all__ = [
    # re-exported from formats._xml for PAGE call sites
    "_detect_namespace",
    "_tag",
    "make_safe_parser",
    "supports_metadata_item",
    "polygon_to_bbox",
]
