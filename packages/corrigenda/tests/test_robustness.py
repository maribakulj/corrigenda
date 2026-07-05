"""Robustness tests for corrigenda parser + validator (L10 Phase 4).

Pins three input-tolerance contracts that a hostile audit found
broken in the pre-L10 codebase:

  - **B3** parser must tolerate empty numeric attributes
    (``WIDTH=""``) without crashing — production ALTO from some
    producers contains them. Pre-fix `int(el.get("WIDTH", 0))`
    raised ``ValueError: invalid literal for int()`` because
    `get("WIDTH", 0)` returned the empty string (the default only
    applies to MISSING keys, not empty values).

  - **B4** validator must reject non-dict raw input as a retryable
    ValueError, not crash with TypeError. Pre-fix
    ``if "lines" not in raw`` raised
    ``TypeError: argument of type 'NoneType' is not iterable`` on
    None or list inputs; TypeError is classified as non-retryable
    by the orchestrator (which only catches `ValueError` /
    `json.JSONDecodeError`), so a provider returning None silently
    burned the entire retry budget AND skipped fallback.

  - **R3** validator must reject whitespace-only ``corrected_text``.
    Pre-fix the rejection was ``corrected_text == ""`` which let a
    single space through; the rewriter would then write
    ``CONTENT="   "`` and obliterate the original word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda.alto.parser import parse_alto_file
from corrigenda.pipeline.validator import validate_llm_response


# ---------------------------------------------------------------------------
# B3 — parser tolerates empty numeric attributes
# ---------------------------------------------------------------------------


def _alto_with_empty_width(tmp_path: Path) -> Path:
    p = tmp_path / "empty_width.xml"
    p.write_bytes(
        b"""<?xml version="1.0"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="" HEIGHT="">
      <PrintSpace>
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="" HEIGHT="">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="" HEIGHT="">
            <String CONTENT="hello" HPOS="0" VPOS="0" WIDTH="" HEIGHT=""/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""
    )
    return p


def test_parser_tolerates_empty_numeric_attributes(tmp_path: Path):
    """L10/B3 — `parse_alto_file` must not crash on a Page/TextBlock/
    TextLine whose WIDTH/HEIGHT/HPOS/VPOS attributes are present but
    empty strings. Pre-fix this raised ``ValueError: invalid literal
    for int(): ''``."""
    pages, _root = parse_alto_file(_alto_with_empty_width(tmp_path), "x.xml")
    assert pages, "parser did not yield any page"
    page = pages[0]
    # Empty attrs should be treated as 0 (the documented default).
    assert page.page_width == 0
    assert page.page_height == 0
    assert page.lines
    line = page.lines[0]
    assert line.coords.width == 0
    assert line.coords.height == 0
    # The actual text content survives the dimension stripping.
    assert line.ocr_text == "hello"


# ---------------------------------------------------------------------------
# B4 — validator handles non-dict raw input as ValueError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_input",
    [
        None,
        [],
        "a string",
        42,
        True,
    ],
    ids=["none", "list", "str", "int", "bool"],
)
def test_validate_llm_response_rejects_non_dict_as_valueerror(bad_input):
    """L10/B4 — pre-fix the validator's first line was
    ``if "lines" not in raw`` which raises TypeError on non-dict
    inputs. TypeError isn't in the orchestrator's retry classifier
    (``(ValueError, json.JSONDecodeError)``), so a provider returning
    None silently fell through to immediate fallback, skipping all
    3 retry attempts.
    """
    with pytest.raises(ValueError):
        validate_llm_response(raw=bad_input, expected_line_ids=["L1"])


# ---------------------------------------------------------------------------
# F5 — parser tolerates float-valued coordinates
# ---------------------------------------------------------------------------


def _alto_with_float_coords(tmp_path: Path) -> Path:
    p = tmp_path / "float_coords.xml"
    p.write_bytes(
        b"""<?xml version="1.0"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="600.0" HEIGHT="800.9">
      <PrintSpace>
        <TextBlock ID="B1" HPOS="10.0" VPOS="20.5" WIDTH="500.0" HEIGHT="30.0">
          <TextLine ID="L1" HPOS="10.0" VPOS="20.5" WIDTH="500.0" HEIGHT="30.0">
            <String CONTENT="hello" HPOS="10.0" VPOS="20.5" WIDTH="100.9" HEIGHT="30.0"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""
    )
    return p


def test_parser_tolerates_float_coordinates(tmp_path: Path):
    """Spec F5 — some ALTO producers emit float coordinates
    (``HPOS="123.0"``, ``HEIGHT="800.9"``). Pre-fix ``int("123.0")``
    raised ``ValueError`` and aborted the whole file. Floats now
    truncate toward zero."""
    pages, _root = parse_alto_file(_alto_with_float_coords(tmp_path), "x.xml")
    assert pages
    page = pages[0]
    assert page.page_width == 600  # 600.0 -> 600
    assert page.page_height == 800  # 800.9 truncates to 800
    line = page.lines[0]
    assert line.coords.vpos == 20  # 20.5 truncates to 20
    assert line.ocr_text == "hello"


def test_int_attr_still_rejects_non_numeric(tmp_path: Path):
    """Spec F5 — the float tolerance must NOT swallow genuinely
    non-numeric attribute values; those still raise ``ValueError``."""
    from lxml import etree

    from corrigenda.alto._ns import _int_attr

    el = etree.Element("TextLine", WIDTH="abc")
    with pytest.raises(ValueError):
        _int_attr(el, "WIDTH")


# ---------------------------------------------------------------------------
# F3 — parser ignores comments / PIs among TextLine children
# ---------------------------------------------------------------------------


def _alto_with_trailing_comment(tmp_path: Path) -> Path:
    p = tmp_path / "trailing_comment.xml"
    # The comment is the LAST child of the TextLine — pre-fix
    # ``etree.QName(last_child.tag)`` raised on it.
    p.write_bytes(
        b"""<?xml version="1.0"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="600" HEIGHT="800">
      <PrintSpace>
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="30">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="500" HEIGHT="30">
            <String CONTENT="bonjour" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="30"/>
            <!-- an OCR-engine annotation left inside the line -->
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""
    )
    return p


def test_parser_tolerates_comment_as_last_textline_child(tmp_path: Path):
    """Spec F3 — a comment (or PI) as the final child of a TextLine
    carries a callable ``tag`` (``etree.Comment``), not a ``str``.
    Pre-fix ``etree.QName(last_child.tag)`` raised and aborted the
    whole file. The parser must skip such nodes."""
    pages, _root = parse_alto_file(_alto_with_trailing_comment(tmp_path), "x.xml")
    assert pages
    line = pages[0].lines[0]
    assert line.ocr_text == "bonjour"
    # No trailing HYP was mistaken from the comment.
    from corrigenda.schemas import HyphenRole

    assert line.hyphen_role == HyphenRole.NONE


# ---------------------------------------------------------------------------
# R3 — validator rejects whitespace-only corrected_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "whitespace_text",
    [" ", "  ", "\t", " \t  ", " "],  # incl. NBSP
    ids=["one_space", "two_spaces", "tab", "mixed", "nbsp"],
)
def test_validate_llm_response_rejects_whitespace_only_corrected_text(
    whitespace_text: str,
):
    """L10/R3 — pre-fix the empty-text check was
    ``corrected_text == ""``. A single space (or NBSP, tab, etc.)
    passed validation; the rewriter would then write
    ``CONTENT="   "`` and obliterate the original word silently.
    """
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": whitespace_text},
        ]
    }
    with pytest.raises(ValueError, match="empty"):
        validate_llm_response(raw=raw, expected_line_ids=["L1"])


# ---------------------------------------------------------------------------
# B6 — hyphen heuristic must not fire on pure-numeric "ranges"
# ---------------------------------------------------------------------------


def _alto_with_two_lines(line1_text: str, line2_text: str, tmp_path: Path) -> Path:
    p = tmp_path / "two_lines.xml"
    p.write_bytes(
        f"""<?xml version="1.0"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace>
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="10">
            <String CONTENT="{line1_text}" HPOS="0" VPOS="0" WIDTH="50" HEIGHT="10"/>
          </TextLine>
          <TextLine ID="L2" HPOS="0" VPOS="20" WIDTH="100" HEIGHT="10">
            <String CONTENT="{line2_text}" HPOS="0" VPOS="20" WIDTH="50" HEIGHT="10"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>""".encode()
    )
    return p


@pytest.mark.parametrize(
    "first_line",
    ["1789-", "n°5-", "-", "42-", "—"],
    ids=["year_range", "num_with_dash", "lone_dash", "pure_numeric", "em_dash"],
)
def test_hyphen_heuristic_does_not_fire_on_non_alpha_trailing_dash(
    first_line: str, tmp_path: Path
):
    """L10/B6 — pre-fix any token ending in `-` was flagged PART1.
    Year ranges like `1789-\\n1799`, list numbers `n°5-`, lone
    dashes, em-dashes all tripped it. The rewriter then emitted a
    phantom HYP element for these false positives. The tightened
    heuristic requires at least one alphabetic char before the
    trailing dash."""
    from corrigenda.schemas import HyphenRole

    p = _alto_with_two_lines(first_line, "1799", tmp_path)
    pages, _root = parse_alto_file(p, "x.xml")
    line1 = pages[0].lines[0]
    assert line1.hyphen_role == HyphenRole.NONE, (
        f"line {line1.line_id!r} was incorrectly tagged "
        f"hyphen_role={line1.hyphen_role.value!r} for ocr_text={first_line!r}. "
        f"The heuristic should only fire on word-break hyphens "
        f"(alphabetic content before the trailing dash)."
    )


def test_hyphen_heuristic_still_fires_on_genuine_word_break(tmp_path: Path):
    """Negative control — a normal word-break hyphen like "écri-" must
    still be flagged PART1 after the heuristic tightening."""
    from corrigenda.schemas import HyphenRole

    p = _alto_with_two_lines("écri-", "vain", tmp_path)
    pages, _root = parse_alto_file(p, "x.xml")
    line1 = pages[0].lines[0]
    assert line1.hyphen_role == HyphenRole.PART1, (
        f"genuine word-break hyphen (ocr_text='écri-') was NOT flagged "
        f"PART1 — heuristic over-tightened. Got {line1.hyphen_role.value!r}."
    )


# ---------------------------------------------------------------------------
# R2 — clean_content strips control + zero-width characters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_text,expected",
    [
        ("hello\x00world", "helloworld"),  # NUL
        ("a​b", "ab"),  # ZWSP
        ("c‌d", "cd"),  # ZWNJ
        ("e‍f", "ef"),  # ZWJ
        ("﻿text", "text"),  # BOM
        ("line1\nline2", "line1line2"),  # NL
        ("\rcarriage", "carriage"),  # CR
        ("tab\there", "tabhere"),  # TAB
        ("c\x01trl\x7fchars", "ctrlchars"),  # SOH + DEL
        ("clean text", "clean text"),  # no-op
    ],
    ids=[
        "nul",
        "zwsp",
        "zwnj",
        "zwj",
        "bom",
        "newline",
        "carriage_return",
        "tab",
        "control_chars",
        "noop",
    ],
)
def test_clean_content_strips_invisible_and_control_chars(raw_text: str, expected: str):
    """L10/R2 — pre-fix `clean_content` only stripped U+00AD. NUL bytes,
    zero-width chars, newlines, tabs, and other C0/C1 control chars
    survived and ended up in ALTO CONTENT attributes — corrupting
    downstream character-indexed consumers and silently violating the
    "no newline in corrected_text" invariant the validator pinned.
    """
    from corrigenda.alto._norm import clean_content

    assert clean_content(raw_text) == expected


def test_clean_content_still_strips_soft_hyphen():
    """Regression guard — the original behaviour (strip U+00AD) must
    still hold after extending the function."""
    from corrigenda.alto._norm import clean_content

    assert clean_content("ca­fé") == "café"


# ---------------------------------------------------------------------------
# R1 — rewriter must NFC-normalize CONTENT before writing
# ---------------------------------------------------------------------------


def test_clean_content_nfc_normalizes_decomposed_input():
    """L10/R1 — the parser NFC-normalizes every CONTENT it READS
    (corrigenda.alto.parser:45). The rewriter's `clean_content` did
    NOT — so an LLM returning `café` in NFD (`cafe\\u0301`) would
    land NFD bytes in the output CONTENT. A subsequent re-parse via
    `reconstruct_textline` (which applies `nfc(...)` again) would
    yield `café` in NFC — equal to the original, but the bytes on
    disk differ from what every other consumer (search index,
    grep, byte-for-byte snapshot) would expect.

    Fix: `clean_content` now applies `nfc(...)` so the WRITE path is
    symmetric with the READ path.
    """
    from corrigenda.alto._norm import clean_content

    nfd = "café"  # 'café' in NFD (e + combining acute)
    nfc_expected = "café"  # NFC precomposed
    assert nfd != nfc_expected, "test setup: NFD/NFC bytes must differ"

    cleaned = clean_content(nfd)
    assert cleaned == nfc_expected, (
        f"clean_content did not NFC-normalize: {cleaned!r} != {nfc_expected!r}. "
        f"Output bytes will differ from the parser's read-side normalisation."
    )


def test_rewriter_writes_nfc_content_from_nfd_correction():
    """L10/R1 integration — write an NFD-form `corrected_text` through
    the full rewriter and verify the on-disk CONTENT is NFC. Without
    this fix the bytes on disk are NFD, silently breaking byte-for-
    byte snapshot tests and downstream byte-indexed consumers."""
    import unicodedata

    from corrigenda.alto.parser import parse_alto_file
    from corrigenda.alto.rewriter import rewrite_alto_file
    from corrigenda.schemas import LineStatus

    # Source ALTO with a single line containing precomposed "café".
    src = b"""<?xml version="1.0"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Layout>
    <Page ID="P1" WIDTH="100" HEIGHT="100">
      <PrintSpace>
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">
          <TextLine ID="L1" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="10">
            <String CONTENT="cafe" HPOS="0" VPOS="0" WIDTH="50" HEIGHT="10"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        tmp.write(src)
        tmp.flush()
        from pathlib import Path as _Path

        src_path = _Path(tmp.name)

    pages, _root = parse_alto_file(src_path, "src.xml")
    # Inject NFD-form correction into the manifest.
    line = pages[0].lines[0]
    line.corrected_text = unicodedata.normalize("NFD", "café")
    line.status = LineStatus.CORRECTED
    assert line.corrected_text != "café", "test setup: corrected_text must be NFD"

    out_bytes, _metrics, _paths = rewrite_alto_file(
        src_path, pages, provider="test", model="test"
    )

    # The CONTENT attribute on disk must be NFC, not NFD.
    assert b"caf\xc3\xa9" in out_bytes, (
        f"rewriter wrote non-NFC bytes for 'café'. Output excerpt: "
        f"{out_bytes[out_bytes.find(b'CONTENT') : out_bytes.find(b'CONTENT') + 80]!r}"
    )
    # Negative: the NFD byte sequence (e + combining acute) must NOT
    # appear in the output where the corrected token landed.
    nfd_bytes = (
        "café".encode("utf-8")
        if "café".encode("utf-8") != b"caf\xc3\xa9"
        else b"cafe\xcc\x81"
    )
    assert nfd_bytes not in out_bytes, (
        f"rewriter leaked NFD bytes into CONTENT: {nfd_bytes!r}"
    )


def test_validate_llm_response_accepts_normal_text():
    """Symmetric — normal text content must still validate (no
    over-zealous rejection from the R3 fix)."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "  hello  "},  # leading/trailing OK
        ]
    }
    result = validate_llm_response(raw=raw, expected_line_ids=["L1"])
    assert result.lines[0].corrected_text == "  hello  "
