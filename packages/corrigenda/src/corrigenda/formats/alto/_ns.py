"""ALTO-specific low-level parser helpers.

The format-agnostic pieces — namespace detection, tag qualification, and
the hardened ``make_safe_parser`` — live in :mod:`corrigenda.formats._xml`
and are re-exported here under their historical private names so ALTO call
sites (``from corrigenda.formats.alto._ns import _tag, …``) are unchanged.
Only ``_int_attr`` (ALTO reads integer geometry from element attributes) is
genuinely ALTO-specific and stays here.
"""

from __future__ import annotations

from lxml import etree

from corrigenda.core._parse import parse_int_tolerant
from corrigenda.formats._xml import (
    detect_namespace as _detect_namespace,
    make_safe_parser as make_safe_parser,
    tag as _tag,
)


def _int_attr(el: etree._Element, name: str, default: int = 0) -> int:
    """Read an integer attribute, tolerating MISSING, EMPTY and FLOAT values.

    Pre-L10 the corrigenda parser used ``int(el.get(name, 0))`` which fails
    on ``WIDTH=""`` (legitimate output from some ALTO producers) because
    ``get(name, 0)`` returns the empty string for present-but-empty attrs —
    the default only fires for MISSING attrs (L10/B3).

    Spec F5 — some ALTO producers emit float-valued coordinates
    (``HPOS="123.0"``, ``WIDTH="12.5"``); floats truncate toward zero. A
    genuinely non-numeric value (``"abc"``) still raises ``ValueError`` (the
    ``strict`` policy) — only blank/float-shaped strings are tolerated. The
    parse policy lives in :func:`corrigenda.core._parse.parse_int_tolerant`,
    shared with the PAGE parser so the two never drift.
    """
    return parse_int_tolerant(el.get(name), default, strict=True)


# ``_detect_namespace`` / ``_tag`` / ``make_safe_parser`` are re-exported
# from ``corrigenda.formats._xml`` — the format-neutral home — so older
# ``formats.alto._ns`` importers keep working. Listed in ``__all__`` to mark
# them as the intentional re-export surface. The canonical import for the
# hardened parser is now ``from corrigenda.formats._xml import make_safe_parser``.
__all__ = ["_detect_namespace", "_tag", "make_safe_parser"]
