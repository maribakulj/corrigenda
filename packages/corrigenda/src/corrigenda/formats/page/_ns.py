"""Shared PAGE XML namespace + geometry helpers.

PAGE (PRImA) ships a family of dated namespaces
(``…/pagecontent/2013-07-15`` through ``2019-07-15`` and later). Parser
and rewriter both detect the namespace from the root tag and address
elements through :func:`_tag`, exactly like the ALTO backend. The
hardened lxml parser is shared verbatim with the ALTO backend — XML
security is format-independent, so there is one canonical
``make_safe_parser`` and every ``etree.parse``/``fromstring`` call site
in ``formats/`` goes through it (enforced by ``test_xml_security.py``).
"""

from __future__ import annotations

# The hardened parser is format-agnostic; reuse the single canonical
# implementation rather than forking a second copy (the security grep
# contract covers every call site under formats/ regardless).
from corrigenda.formats.alto._ns import make_safe_parser  # noqa: F401  re-exported

#: PAGE namespace URIs are dated; anything from this year onward carries
#: the richer ``Metadata/MetadataItem`` provenance slot (P7). Earlier
#: schemas fall back to ``Metadata/Comments``.
_METADATA_ITEM_MIN_YEAR = 2019


def _detect_namespace(root: object) -> str:
    """Return the namespace URI from the root tag, or '' if none.

    Mirrors the ALTO detector: defensive against a tag that opens with
    ``{`` but has no closing brace.
    """
    tag = getattr(root, "tag", "")
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return ""


def _tag(local: str, ns: str) -> str:
    return f"{{{ns}}}{local}" if ns else local


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
        try:
            xs.append(int(float(x_str)))
            ys.append(int(float(y_str)))
        except ValueError:
            continue
    if not xs or not ys:
        return 0, 0, 0, 0
    hpos, vpos = min(xs), min(ys)
    return hpos, vpos, max(xs) - hpos, max(ys) - vpos


__all__ = [
    "make_safe_parser",
    "supports_metadata_item",
    "polygon_to_bbox",
]
