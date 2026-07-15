"""Plan V4.2 phase 2 — XML fuzzing of the parse boundary.

The property suite (test_properties_hypothesis.py) generates VALID ALTO
and asserts pipeline invariants. This file feeds the parsers HOSTILE
input — malformed XML, encoding mismatches, non-numeric coordinates,
degenerate polygons, incoherent SUBS_* — and asserts the §8.4 error
contract at the library's front door:

    parse either SUCCEEDS, or raises a classified CorrectionError
    (ParseError family). Never an unclassified lxml / OS / ValueError,
    never a TypeError-shaped crash.

Every generator here produces input a caller could actually upload;
nothing relies on internal APIs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from corrigenda.errors import CorrectionError
from corrigenda.formats.alto.parser import (
    build_document_manifest as build_alto_manifest,
)
from corrigenda.formats.page.parser import (
    build_document_manifest as build_page_manifest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bytes_alto(payload: bytes) -> None:
    """Write payload to disk and parse it as ALTO; classified errors OK."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fuzz.xml"
        p.write_bytes(payload)
        try:
            build_alto_manifest([(p, "fuzz.xml")])
        except CorrectionError:
            pass  # classified — the contract holds


def _parse_bytes_page(payload: bytes) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fuzz.xml"
        p.write_bytes(payload)
        try:
            build_page_manifest([(p, "fuzz.xml")])
        except CorrectionError:
            pass


# An attribute value that may or may not be a number: the strict
# coordinate policy must classify the failure, not leak a ValueError.
_ATTR_VALUE = st.one_of(
    st.integers(-(10**9), 10**9).map(str),
    st.floats(allow_nan=True, allow_infinity=True).map(str),
    st.just(""),
    st.text(max_size=12),
)

# Free text that may contain XML metacharacters once escaped — the
# builder escapes it, so this fuzzes CONTENT, not well-formedness.
_FREE_TEXT = st.text(max_size=40)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# 1. Arbitrary bytes — the rawest contract
# ---------------------------------------------------------------------------


@settings(max_examples=80, deadline=None)
@given(payload=st.binary(max_size=2048))
def test_arbitrary_bytes_never_crash_alto_unclassified(payload: bytes) -> None:
    _parse_bytes_alto(payload)


@settings(max_examples=80, deadline=None)
@given(payload=st.binary(max_size=2048))
def test_arbitrary_bytes_never_crash_page_unclassified(payload: bytes) -> None:
    _parse_bytes_page(payload)


# ---------------------------------------------------------------------------
# 2. Truncations & mutations of a real document
# ---------------------------------------------------------------------------

_VALID_ALTO = b"""<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout><Page ID="P1" WIDTH="1000" HEIGHT="1400">
    <PrintSpace>
      <TextBlock ID="B1" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="200">
        <TextLine ID="L1" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="40">
          <String CONTENT="premiere" HPOS="10" VPOS="10" WIDTH="200" HEIGHT="40"/>
        </TextLine>
        <TextLine ID="L2" HPOS="10" VPOS="60" WIDTH="900" HEIGHT="40">
          <String CONTENT="seconde" HPOS="10" VPOS="60" WIDTH="200" HEIGHT="40"/>
        </TextLine>
      </TextBlock>
    </PrintSpace>
  </Page></Layout>
</alto>
"""


@settings(max_examples=80, deadline=None)
@given(cut=st.integers(0, len(_VALID_ALTO) - 1))
def test_truncated_alto_is_classified(cut: int) -> None:
    """Any prefix of a valid document parses or fails classified."""
    _parse_bytes_alto(_VALID_ALTO[:cut])


@settings(max_examples=80, deadline=None)
@given(
    pos=st.integers(0, len(_VALID_ALTO) - 1),
    byte=st.integers(0, 255),
)
def test_single_byte_mutation_is_classified(pos: int, byte: int) -> None:
    """Flipping one byte anywhere must never produce an unclassified crash."""
    mutated = bytes(_VALID_ALTO[:pos]) + bytes([byte]) + bytes(_VALID_ALTO[pos + 1 :])
    _parse_bytes_alto(mutated)


@settings(max_examples=40, deadline=None)
@given(
    encoding_label=st.sampled_from(
        ["UTF-8", "UTF-16", "ISO-8859-1", "koi8-r", "bogus-enc"]
    )
)
def test_encoding_declaration_mismatch_is_classified(encoding_label: str) -> None:
    """Declared encoding ≠ actual bytes: parse or ParseError, no leak."""
    body = _VALID_ALTO.replace(
        b'encoding="UTF-8"', f'encoding="{encoding_label}"'.encode()
    )
    _parse_bytes_alto(body)


# ---------------------------------------------------------------------------
# 3. Structured ALTO fuzz — hostile attributes & SUBS_* combinations
# ---------------------------------------------------------------------------


@st.composite
def hostile_alto(draw: st.DrawFn) -> bytes:
    """Well-formed XML, hostile SEMANTICS: random coordinate values,
    random/incoherent SUBS_TYPE / SUBS_CONTENT / HYP, arbitrary text."""
    n_lines = draw(st.integers(1, 5))
    lines: list[str] = []
    for i in range(n_lines):
        subs_type = draw(
            st.sampled_from(["", "HypPart1", "HypPart2", "HYPHEN", "hyppart1", "junk"])
        )
        subs_content = draw(st.one_of(st.just(""), _FREE_TEXT))
        content = _esc(draw(_FREE_TEXT))
        hpos = draw(_ATTR_VALUE)
        attrs = f'HPOS="{_esc(hpos)}" VPOS="{i * 50}" WIDTH="100" HEIGHT="40"'
        subs = ""
        if subs_type:
            subs = f' SUBS_TYPE="{subs_type}"'
            if subs_content:
                subs += f' SUBS_CONTENT="{_esc(subs_content)}"'
        hyp = '<HYP CONTENT="-"/>' if draw(st.booleans()) else ""
        maybe_id = f'ID="L{i}"' if draw(st.booleans()) else ""
        lines.append(
            f"<TextLine {maybe_id} {attrs}>"
            f'<String CONTENT="{content}"{subs} {attrs}/>{hyp}'
            f"</TextLine>"
        )
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">'
        '<Layout><Page ID="P1" WIDTH="1000" HEIGHT="1400"><PrintSpace>'
        '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="900" HEIGHT="900">'
        + "".join(lines)
        + "</TextBlock></PrintSpace></Page></Layout></alto>"
    )
    return doc.encode("utf-8")


@settings(max_examples=120, deadline=None)
@given(payload=hostile_alto())
def test_hostile_alto_semantics_are_classified(payload: bytes) -> None:
    _parse_bytes_alto(payload)


# ---------------------------------------------------------------------------
# 4. Structured PAGE fuzz — degenerate polygons
# ---------------------------------------------------------------------------

_POINTS = st.one_of(
    st.just(""),
    st.just("0,0"),
    st.just("1,2 3"),
    st.just("a,b c,d"),
    st.just("-5,-5 -1,-1"),
    st.just("999999999,999999999 0,0"),
    st.text(alphabet="0123456789,- .", max_size=30),
)


@st.composite
def hostile_page(draw: st.DrawFn) -> bytes:
    n_lines = draw(st.integers(1, 4))
    lines = []
    for i in range(n_lines):
        pts = draw(_POINTS)
        text = _esc(draw(_FREE_TEXT))
        lines.append(
            f'<TextLine id="l{i}"><Coords points="{_esc(pts)}"/>'
            f"<TextEquiv><Unicode>{text}</Unicode></TextEquiv></TextLine>"
        )
    region_pts = draw(_POINTS)
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">'
        f'<Page imageFilename="img.jpg" imageWidth="{_esc(draw(_ATTR_VALUE))}"'
        f' imageHeight="1400">'
        f'<TextRegion id="r1"><Coords points="{_esc(region_pts)}"/>'
        + "".join(lines)
        + "</TextRegion></Page></PcGts>"
    )
    return doc.encode("utf-8")


@settings(max_examples=120, deadline=None)
@given(payload=hostile_page())
def test_hostile_page_polygons_are_classified(payload: bytes) -> None:
    _parse_bytes_page(payload)
