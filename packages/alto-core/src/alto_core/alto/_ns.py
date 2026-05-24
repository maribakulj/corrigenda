"""Shared ALTO XML namespace helpers.

Both parser.py and rewriter.py need identical _detect_namespace / _tag
logic. Centralising here prevents the two copies drifting apart.
"""

from __future__ import annotations

from lxml import etree


def _detect_namespace(root: etree._Element) -> str:
    """Return the namespace URI from the root tag, or '' if none.

    Defensive against malformed tags that start with '{' but lack '}'
    (would otherwise raise ValueError in the callers).
    """
    tag: str = root.tag
    if tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return ""


def _tag(local: str, ns: str) -> str:
    return f"{{{ns}}}{local}" if ns else local


# --- __all__ (Stage 3 audit remediation) ---
__all__: list[str] = []
