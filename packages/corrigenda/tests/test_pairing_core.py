"""Unit tests for the format-agnostic hyphen-pair core (core/pairing.py).

The linking functions are already exercised end-to-end through the ALTO
parser (and byte-parity gate), so these tests focus on the pieces that
ALTO does NOT drive: the wider ``trailing_hyphen_char`` repertoire used by
PAGE (P5) and the pure linker's behaviour on hand-built manifests.
"""

from __future__ import annotations

from corrigenda.core.pairing import (
    HYPHEN_CHARS,
    disambiguate_page_ids,
    link_hyphen_pairs,
    trailing_hyphen_char,
)
from corrigenda.core.schemas import Coords, HyphenRole, LineManifest, PageManifest


def _line(line_id: str, text: str, page_id: str = "p1", vpos: int = 0) -> LineManifest:
    # P1-2 — the default PairingPolicy vets geometry, so consecutive test
    # lines must be stacked realistically (pass vpos) instead of all
    # sitting at the same coordinates.
    return LineManifest(
        line_id=line_id,
        page_id=page_id,
        block_id="b1",
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=0, vpos=vpos, width=100, height=10),
        ocr_text=text,
    )


# --- trailing_hyphen_char (P5 repertoire) ---------------------------------


def test_trailing_hyphen_char_matches_each_repertoire_member():
    assert trailing_hyphen_char("infor-", HYPHEN_CHARS) == "-"
    assert trailing_hyphen_char("appro¬", HYPHEN_CHARS) == "¬"
    assert trailing_hyphen_char("Hiſtoi⸗", HYPHEN_CHARS) == "⸗"
    assert trailing_hyphen_char("écri­", HYPHEN_CHARS) == "­"


def test_trailing_hyphen_char_requires_alpha_before_dash():
    # Year ranges and list markers are NOT word-break hyphens.
    assert trailing_hyphen_char("1789-", HYPHEN_CHARS) is None
    assert trailing_hyphen_char("n°5-", HYPHEN_CHARS) is None
    # A bare dash token is not a hyphen either.
    assert trailing_hyphen_char("-", HYPHEN_CHARS) is None


def test_trailing_hyphen_char_uses_last_token_only():
    assert trailing_hyphen_char("word ends here", HYPHEN_CHARS) is None
    assert trailing_hyphen_char("mid- word", HYPHEN_CHARS) is None
    assert trailing_hyphen_char("some appro¬", HYPHEN_CHARS) == "¬"


def test_trailing_hyphen_char_empty():
    assert trailing_hyphen_char("", HYPHEN_CHARS) is None
    assert trailing_hyphen_char("   ", HYPHEN_CHARS) is None


# --- link_hyphen_pairs (pure linker) --------------------------------------


def test_link_pairs_marks_next_line_part2():
    part1 = _line("l1", "appro-")
    part1.hyphen_role = HyphenRole.PART1
    part2 = _line("l2", "bation", vpos=12)
    link_hyphen_pairs([part1, part2])
    assert part2.hyphen_role == HyphenRole.PART2
    assert part1.hyphen_pair_line_id == "l2"
    assert part2.hyphen_pair_line_id == "l1"


def test_link_pairs_noop_when_no_part1():
    a = _line("l1", "hello")
    b = _line("l2", "world")
    link_hyphen_pairs([a, b])
    assert a.hyphen_pair_line_id is None
    assert b.hyphen_role == HyphenRole.NONE


# --- disambiguate_page_ids ------------------------------------------------


def test_disambiguate_prefixes_colliding_page_ids():
    def _page(src: str) -> PageManifest:
        line = _line("tl1", "x", page_id="P1")
        line.hyphen_pair_page_id = "P1"
        return PageManifest(
            page_id="P1",
            source_file=src,
            page_index=0,
            page_width=10,
            page_height=10,
            blocks=[],
            lines=[line],
        )

    a = ("a.xml", [_page("a.xml")])
    b = ("b.xml", [_page("b.xml")])
    disambiguate_page_ids([a, b])
    assert a[1][0].page_id == "a.xml::P1"
    assert b[1][0].page_id == "b.xml::P1"
    # Intra-page hyphen refs are rewritten to the qualified id.
    assert a[1][0].lines[0].hyphen_pair_page_id == "a.xml::P1"
