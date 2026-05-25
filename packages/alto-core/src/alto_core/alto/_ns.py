"""Shared ALTO XML namespace + low-level parser helpers.

Both parser.py and rewriter.py need identical _detect_namespace / _tag
logic plus the SAME hardened lxml parser configuration. Centralising
here prevents the parser config drifting between call sites — a class
of bug a hostile audit caught pre-L10 (the rewriter was using lxml's
default parser, exposing it to entity-expansion DoS).
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


def _int_attr(el: etree._Element, name: str, default: int = 0) -> int:
    """Read an integer attribute, tolerating MISSING and EMPTY values.

    Pre-L10 the alto-core parser used ``int(el.get(name, 0))`` which
    fails on ``WIDTH=""`` (legitimate output from some ALTO
    producers) because `get(name, 0)` returns the empty string for
    present-but-empty attrs — the default only fires for MISSING
    attrs (L10/B3).

    Non-integer values still raise ValueError as before — only the
    empty-string case is normalised to the default.
    """
    raw = el.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


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
    ``packages/alto-core/tests/test_xml_security.py`` trips on any
    call site that doesn't.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
    )


# --- __all__ (Stage 3 audit remediation) ---
# `make_safe_parser` stays reachable via the private module path; the
# 7 backend shims that used to forward this module's symbols were
# deleted in L8/L9 — the canonical import is now
# ``from alto_core.alto._ns import make_safe_parser``.
__all__: list[str] = []
