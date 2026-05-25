"""Robustness tests for alto-core parser + validator (L10 Phase 4).

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

from alto_core.alto.parser import parse_alto_file
from alto_core.pipeline.validator import validate_llm_response


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
