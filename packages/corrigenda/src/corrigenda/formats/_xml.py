"""Format-agnostic XML helpers shared by every transcription format.

lxml lives here — so this module is NOT part of the pure ``core`` (the
import-contract test allows ``formats`` to import lxml) — but it is NOT a
format either: namespace detection, tag qualification, and the hardened
parser are identical for ALTO, PAGE, and any future format.

Homing them here keeps the format packages *siblings* — ``alto → _xml ←
page`` — instead of making one an accidental base that the other reaches
sideways into (the former ``page → alto`` edge, which meant deleting or
refactoring ALTO would break PAGE). Each format's ``_ns`` re-exports these
three under its existing private names, so call sites are unchanged.
"""

from __future__ import annotations

from lxml import etree


def detect_namespace(root: object) -> str:
    """Return the namespace URI from a root element's tag, or '' if none.

    Defensive against a tag that opens with ``{`` but has no closing brace,
    and against a non-element ``root`` (``getattr`` fallback) — works for
    both the ALTO and PAGE parse/rewrite entry points.
    """
    tag = getattr(root, "tag", "")
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return ""


def tag(local: str, ns: str) -> str:
    """Qualify a local tag name with a namespace (Clark notation)."""
    return f"{{{ns}}}{local}" if ns else local


def make_safe_parser() -> etree.XMLParser:
    """Return an lxml parser hardened against XXE / SSRF / entity-amplification.

    The four flags together neutralise the well-known XML attack surface:

      - ``resolve_entities=False`` — do not expand ``&entity;`` references.
        Defeats internal-entity amplification ("billion laughs") and any
        residual external-entity leak across lxml versions.
      - ``no_network=True`` — refuse to fetch external DTDs / entities.
        Defeats SSRF via ``<!DOCTYPE x SYSTEM "http://...">``.
      - ``load_dtd=False`` — do not load any DTD (inline or external).
        Defence in depth on top of ``no_network``.
      - ``dtd_validation=False`` — do not validate against a DTD. Default
        already; pinned here for clarity (a future maintainer flipping
        validation on would silently re-enable DTD loading).

    Returns a FRESH parser instance per call: lxml parsers are not
    documented as thread-safe and the construction cost is microseconds.

    Use this for EVERY ``etree.parse`` / ``etree.fromstring`` call that
    touches user-controlled XML — the grep-based contract test in
    ``packages/corrigenda/tests/test_xml_security.py`` trips on any call
    site under ``formats/`` that doesn't.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
    )


__all__ = ["detect_namespace", "tag", "make_safe_parser"]
