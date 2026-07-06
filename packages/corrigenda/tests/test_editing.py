"""Edit protocol tests (spec §4): types, normalisation, application, E1–E6.

Includes the byte-parity proof that re-expressing a whole-line correction
as a ``replace_line`` EditScript and applying it reproduces the exact same
corrected text — and therefore the exact same rewritten ALTO bytes — as the
historical direct path.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from corrigenda.core.editing import (
    EditScript,
    MatchAnchor,
    RangeAnchor,
    ReplaceLine,
    ReplaceSpan,
    apply_edit_script,
    normalize_anchor,
    replace_line_script,
)
from corrigenda.core.schemas import GuardConfig, HyphenRole, LineManifest, Coords
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.formats.alto.rewriter import rewrite_alto_file

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples"


def _line(line_id: str, role: HyphenRole = HyphenRole.NONE) -> LineManifest:
    return LineManifest(
        line_id=line_id,
        page_id="p",
        block_id="b",
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=0, vpos=0, width=10, height=10),
        ocr_text="x",
        hyphen_role=role,
    )


# ---------------------------------------------------------------------------
# Anchor normalisation (§4.3)
# ---------------------------------------------------------------------------


def test_range_anchor_bounds():
    assert normalize_anchor(RangeAnchor(start=0, end=3), "hello")[0] == RangeAnchor(
        start=0, end=3
    )
    assert normalize_anchor(RangeAnchor(start=3, end=2), "hello")[0] is None
    assert normalize_anchor(RangeAnchor(start=0, end=99), "hello")[0] is None


def test_match_anchor_unique():
    rng, reason = normalize_anchor(MatchAnchor(match="lo"), "hello")
    assert rng == RangeAnchor(start=3, end=5) and reason is None


def test_match_anchor_not_found():
    rng, reason = normalize_anchor(MatchAnchor(match="zz"), "hello")
    assert rng is None and reason == "anchor_not_found"


def test_match_anchor_ambiguous_default_occurrence():
    rng, reason = normalize_anchor(MatchAnchor(match="l"), "hello")  # 2 matches
    assert rng is None and reason == "anchor_ambiguous"


def test_match_anchor_explicit_occurrence():
    rng, _ = normalize_anchor(MatchAnchor(match="l", occurrence=1), "hello")
    assert rng == RangeAnchor(start=3, end=4)


def test_match_anchor_occurrence_out_of_range():
    rng, reason = normalize_anchor(MatchAnchor(match="l", occurrence=9), "hello")
    assert rng is None and reason == "anchor_out_of_range"


# ---------------------------------------------------------------------------
# replace_line (E1/E3/conflict; NO E4/E5)
# ---------------------------------------------------------------------------


def test_replace_line_basic():
    script = EditScript(ops=[ReplaceLine(line_id="l1", text="corrected")])
    res = apply_edit_script(script, {"l1": "original"})
    assert res.text_by_id == {"l1": "corrected"}
    assert res.rejected == []


def test_replace_line_rejects_empty_and_newline():
    res = apply_edit_script(
        EditScript(ops=[ReplaceLine(line_id="l1", text="   ")]), {"l1": "x"}
    )
    assert res.text_by_id == {} and res.rejected[0].reason == "e3_empty"
    res2 = apply_edit_script(
        EditScript(ops=[ReplaceLine(line_id="l1", text="a\nb")]), {"l1": "x"}
    )
    assert res2.rejected[0].reason == "e3_newline"


def test_replace_line_conflict_when_duplicated_or_mixed():
    res = apply_edit_script(
        EditScript(
            ops=[
                ReplaceLine(line_id="l1", text="a"),
                ReplaceLine(line_id="l1", text="b"),
            ]
        ),
        {"l1": "x"},
    )
    assert res.text_by_id == {} and res.rejected[0].reason == "conflict"


def test_e1_unknown_or_out_of_chunk_line_rejected():
    res = apply_edit_script(
        EditScript(ops=[ReplaceLine(line_id="l9", text="a")]),
        {"l1": "x"},
        chunk_line_ids={"l1"},
    )
    assert res.text_by_id == {} and res.rejected[0].reason == "e1_unknown_line"


# ---------------------------------------------------------------------------
# replace_span (E2/E4/E5)
# ---------------------------------------------------------------------------


def test_replace_span_single_range():
    res = apply_edit_script(
        EditScript(
            ops=[ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=0, end=4), text="Hello")]
        ),
        {"l1": "helo world"},
    )
    assert res.text_by_id == {"l1": "Hello world"}


def test_replace_span_deletion_allowed_if_line_nonempty():
    res = apply_edit_script(
        EditScript(
            ops=[ReplaceSpan(line_id="l1", anchor=MatchAnchor(match=" extra"), text="")]
        ),
        {"l1": "word extra"},
    )
    assert res.text_by_id == {"l1": "word"}


def test_replace_span_right_to_left_multiple():
    res = apply_edit_script(
        EditScript(
            ops=[
                ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=0, end=1), text="X"),
                ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=6, end=7), text="Y"),
            ]
        ),
        {"l1": "abcdefg"},
    )
    assert res.text_by_id == {"l1": "XbcdefY"}


def test_e2_overlap_rejected():
    res = apply_edit_script(
        EditScript(
            ops=[
                ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=0, end=4), text="Z"),
                ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=2, end=6), text="Q"),
            ]
        ),
        {"l1": "abcdefgh"},
    )
    # First applies; the overlapping second is rejected.
    assert any(r.reason == "e2_overlap" for r in res.rejected)


def test_e4_span_growth_ratio_rejected():
    res = apply_edit_script(
        EditScript(
            ops=[ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=0, end=1), text="X" * 100)]
        ),
        {"l1": "abc"},
        guard_config=GuardConfig(edit_span_max_growth_ratio=4.0),
    )
    assert res.text_by_id == {} and any(r.reason == "e4_span_growth" for r in res.rejected)


def test_e4_line_budget_rejected():
    res = apply_edit_script(
        EditScript(
            ops=[ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=0, end=3), text="abcdefghij")]
        ),
        {"l1": "abcxxxxxxx"},
        guard_config=GuardConfig(edit_line_max_changed_chars=3),
    )
    assert any(r.reason == "e4_line_budget" for r in res.rejected)


def test_e5_hyphen_forward_line_must_keep_trailing_hyphen():
    # Removing the trailing hyphen of a PART1 line is rejected (E5).
    res = apply_edit_script(
        EditScript(
            ops=[ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=4, end=5), text="")]
        ),
        {"l1": "word-"},
        line_by_id={"l1": _line("l1", HyphenRole.PART1)},
    )
    assert res.text_by_id == {} and any(r.reason == "e5_hyphen" for r in res.rejected)


def test_e5_hyphen_ok_when_hyphen_kept():
    res = apply_edit_script(
        EditScript(
            ops=[ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=0, end=2), text="Wo")]
        ),
        {"l1": "word-"},
        line_by_id={"l1": _line("l1", HyphenRole.PART1)},
    )
    assert res.text_by_id == {"l1": "Word-"}


# ---------------------------------------------------------------------------
# Byte-parity: replace_line re-expression == historical direct path
# ---------------------------------------------------------------------------


def _scripted(i: int, text: str) -> str:
    words = text.split()
    if not words:
        return text
    if i % 7 == 0:
        return text + " zz"
    if i % 3 == 0 and "e" in words[0]:
        return " ".join([words[0].replace("e", "3", 1)] + words[1:])
    return text


@pytest.mark.parametrize("filename", ["sample.xml", "X0000002.xml"])
def test_replace_line_reexpression_is_byte_identical(filename: str):
    xml_path = _EXAMPLES / filename
    doc = build_document_manifest([(xml_path, xml_path.name)])

    # Direct path — the historical {line_id: corrected_text} map.
    direct: dict[str, str] = {}
    i = 0
    for page in doc.pages:
        for lm in page.lines:
            direct[lm.line_id] = _scripted(i, lm.ocr_text)
            i += 1

    # Protocol path — re-express as replace_line, apply, compare.
    result = apply_edit_script(replace_line_script(direct), {k: k for k in direct})
    assert result.text_by_id == direct, "replace_line re-expression changed the text map"

    # And the rewritten bytes are identical whichever map feeds the rewriter.
    def _bytes(text_map: dict[str, str]) -> str:
        for page in doc.pages:
            for lm in page.lines:
                lm.corrected_text = text_map[lm.line_id]
        xml_bytes, _m, _p = rewrite_alto_file(xml_path, doc.pages, "test", "mock")
        return hashlib.sha256(xml_bytes).hexdigest()

    assert _bytes(direct) == _bytes(result.text_by_id)
