"""P1-2 — geometric default pairing policy.

The historical default accepted *every* sequential candidate: on layouts
whose serialisation order diverges from reading order, a PART1 at the
bottom of a column could pair with a note in the margin, a block far
below, or an out-of-order line — and the mis-pair only surfaced (at
best) in the downstream guards, long after the wrong partner had shaped
the LLM context.

The new default vets heuristic pairs at pairing time:

  * same block — candidate must sit below, within a few line heights;
  * cross-block — downward continuation needs horizontal overlap (next
    block, same column); an upward jump must be horizontally disjoint
    (start of another column, direction-agnostic so RTL works);
  * explicit (engine-asserted) pairs and degenerate geometry are always
    trusted; cross-page seams too (last line → first line by
    construction);
  * ``geometric_checks=False`` restores the historical behaviour.
"""

from __future__ import annotations

from corrigenda.core.pairing import link_hyphen_pairs
from corrigenda.core.schemas import (
    Coords,
    HyphenRole,
    LineManifest,
    PairingPolicy,
)


def _lm(
    line_id: str,
    *,
    hpos: int = 0,
    vpos: int = 0,
    width: int = 400,
    height: int = 20,
    block_id: str = "b1",
    page_id: str = "p1",
    text: str = "texte du corps-",
) -> LineManifest:
    return LineManifest(
        line_id=line_id,
        page_id=page_id,
        block_id=block_id,
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=hpos, vpos=vpos, width=width, height=height),
        ocr_text=text,
    )


def _pair(part1: LineManifest, candidate: LineManifest) -> bool:
    part1.hyphen_role = HyphenRole.PART1
    link_hyphen_pairs([part1, candidate])
    return part1.hyphen_pair_line_id == candidate.line_id


# ---------------------------------------------------------------------------
# Same block
# ---------------------------------------------------------------------------


def test_same_block_next_line_below_is_accepted():
    assert _pair(_lm("l1", vpos=100), _lm("l2", vpos=124))


def test_same_block_far_below_is_rejected():
    # > 3 line heights below: segmentation jump, not a continuation.
    assert not _pair(_lm("l1", vpos=100, height=20), _lm("l2", vpos=200))


def test_same_block_candidate_above_is_rejected():
    assert not _pair(_lm("l1", vpos=100), _lm("l2", vpos=40))


def test_same_block_slight_overlap_tolerated():
    # Boxes overlapping by a few units (skew) stay within the rise tolerance.
    assert _pair(_lm("l1", vpos=100, height=20), _lm("l2", vpos=112))


# ---------------------------------------------------------------------------
# Cross-block, same page
# ---------------------------------------------------------------------------


def test_next_block_same_column_is_accepted():
    # Downward, horizontally overlapping: paragraph break inside a column.
    assert _pair(
        _lm("l1", block_id="b1", hpos=0, vpos=500),
        _lm("l2", block_id="b2", hpos=0, vpos=540),
    )


def test_column_jump_is_accepted():
    # Bottom of column 1 → top of column 2: upward AND horizontally
    # disjoint. The legitimate multicolumn flow must survive.
    assert _pair(
        _lm("l1", block_id="col1", hpos=0, width=450, vpos=900),
        _lm("l2", block_id="col2", hpos=500, width=450, vpos=50),
    )


def test_marginal_note_at_same_height_is_rejected():
    # Horizontally disjoint but NOT an upward column jump: a note sitting
    # beside the column is not a reading continuation.
    assert not _pair(
        _lm("l1", block_id="body", hpos=0, width=450, vpos=300),
        _lm("l2", block_id="margin", hpos=500, width=200, vpos=300),
    )


def test_unrelated_block_far_below_other_column_is_rejected():
    assert not _pair(
        _lm("l1", block_id="col1", hpos=0, width=450, vpos=100),
        _lm("l2", block_id="ad", hpos=500, width=450, vpos=800),
    )


def test_upward_same_column_is_rejected():
    # Upward with horizontal overlap = serialisation-order error, not flow.
    assert not _pair(
        _lm("l1", block_id="b2", hpos=0, vpos=500),
        _lm("l2", block_id="b1", hpos=0, vpos=100),
    )


def test_rtl_column_jump_is_accepted():
    # Right-to-left layouts jump to a column on the LEFT: the disjointness
    # test is direction-agnostic.
    assert _pair(
        _lm("l1", block_id="col1", hpos=500, width=450, vpos=900),
        _lm("l2", block_id="col2", hpos=0, width=450, vpos=50),
    )


# ---------------------------------------------------------------------------
# Trust rules
# ---------------------------------------------------------------------------


def test_explicit_pair_bypasses_geometry():
    part1 = _lm("l1", vpos=100)
    part1.hyphen_role = HyphenRole.PART1
    part1.hyphen_source_explicit = True  # engine-asserted (SUBS_TYPE/HYP)
    candidate = _lm("l2", vpos=900, hpos=500)  # geometrically implausible
    link_hyphen_pairs([part1, candidate])
    assert part1.hyphen_pair_line_id == "l2"


def test_explicit_part2_side_bypasses_geometry():
    part1 = _lm("l1", vpos=100)
    part1.hyphen_role = HyphenRole.PART1
    candidate = _lm("l2", vpos=900, hpos=500)
    candidate.hyphen_role = HyphenRole.PART2
    candidate.hyphen_source_explicit = True
    link_hyphen_pairs([part1, candidate])
    assert part1.hyphen_pair_line_id == "l2"


def test_degenerate_geometry_is_trusted():
    # Coordinate-less exports (all-zero boxes) must keep hyphenation alive.
    assert _pair(
        _lm("l1", width=0, height=0),
        _lm("l2", width=0, height=0),
    )


def test_cross_page_pair_is_trusted():
    assert _pair(
        _lm("l1", page_id="p1", vpos=900),
        _lm("l2", page_id="p2", vpos=50),
    )


def test_escape_hatch_restores_historical_behaviour():
    policy = PairingPolicy(geometric_checks=False)
    part1 = _lm("l1", block_id="body", hpos=0, width=450, vpos=300)
    part1.hyphen_role = HyphenRole.PART1
    candidate = _lm("l2", block_id="margin", hpos=500, width=200, vpos=300)
    link_hyphen_pairs([part1, candidate], policy)
    assert part1.hyphen_pair_line_id == "l2"
