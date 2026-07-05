"""Pin the slow-path token geometry distribution (spec F6).

``_compute_geometry`` splits a corrected line's width across its tokens.
Pre-fix, the 0.6 weight applied to space tokens did NOT enter the total
used to compute the per-unit width, so every space's shortfall was dumped
onto the last token via a single ``correction`` term — inflating it.

These tests pin the corrected contract:
  - widths sum EXACTLY to the line width (no drift);
  - the 0.6 space weight is consistent on both sides, so two identical
    words separated by a space get identical widths (the old code gave
    the trailing word all the accumulated space deficit);
  - the final token only absorbs residual rounding, never a large slug.
"""

from __future__ import annotations

from alto_core.alto.rewriter import _compute_geometry, _tokenize


def _widths(text: str, hpos: int, width: int) -> list[int]:
    return [w for _t, _h, w in _compute_geometry(hpos, width, _tokenize(text))]


def test_widths_sum_exactly_to_line_width():
    for text, width in [
        ("hello world", 200),
        ("a b c d e", 173),
        ("Régnait de mille sept cent", 421),
        ("un", 40),
        ("le mot suivant est long", 999),
    ]:
        widths = _widths(text, hpos=10, width=width)
        assert sum(widths) == width, f"{text!r}: {sum(widths)} != {width}"


def test_hpos_is_contiguous_from_start():
    tokens = _tokenize("hello world foo")
    geo = _compute_geometry(50, 300, tokens)
    cursor = 50
    for _t, h, w in geo:
        assert h == cursor
        cursor += w


def test_identical_words_around_a_space_get_equal_widths():
    """Spec F6 — two identical words separated by one space must render
    with the same width. Pre-fix the trailing word absorbed the space
    deficit and came out wider."""
    widths = _widths("abcd abcd", hpos=0, width=100)
    # tokens: ["abcd", " ", "abcd"]
    assert len(widths) == 3
    assert widths[0] == widths[2], f"asymmetric: {widths}"


def test_final_token_not_inflated_by_space_deficit():
    """The last token must not be dramatically larger than an equal-length
    earlier token purely because of space handling."""
    # "aa bb cc" — three equal words, two spaces.
    widths = _widths("aa bb cc", hpos=0, width=100)
    word_widths = [widths[0], widths[2], widths[4]]
    spread = max(word_widths) - min(word_widths)
    # Equal-length words should differ only by rounding (<= 1px).
    assert spread <= 1, f"word widths not balanced: {word_widths}"


def test_all_widths_at_least_one_at_the_boundary():
    """Tight line where width equals the token count: every token still
    gets at least 1px and the sum invariant holds."""
    # "a b c" tokenises to 5 tokens; width 5 is the tightest all-1 case.
    widths = _widths("a b c", hpos=0, width=5)
    assert len(widths) == 5
    assert all(w >= 1 for w in widths), widths
    assert sum(widths) == 5


def test_degenerate_width_below_token_count_pins_floor_behaviour():
    """Post-audit F6 pin — when width < token count the exact-sum
    invariant is mathematically unsatisfiable with all-≥1 widths: the
    min-1 floor wins and the sum settles at the token count. Any feasible
    width keeps the exact sum, spread across several donors if needed."""
    # 11 tokens ("a b c d e f"), width 7 → infeasible: all 1, sum 11.
    widths = _widths("a b c d e f", hpos=0, width=7)
    assert len(widths) == 11
    assert all(w >= 1 for w in widths), widths
    assert sum(widths) == 11  # floor wins, documented

    # Feasible tight case: sum must be EXACT even when several tokens
    # need flooring (deficit spread over multiple donors).
    widths2 = _widths("aaaaaaaa b c", hpos=0, width=12)
    assert all(w >= 1 for w in widths2), widths2
    assert sum(widths2) == 12
