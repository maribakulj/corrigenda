"""PairingPolicy — injectable hyphen-pairing seam (spec F7).

Pins:
  - the default policy reproduces the historical purely-sequential
    pairing (the next line is always accepted);
  - a restrictive policy (same-block-only or a vertical-gap cap) breaks a
    pair the default would form, WITHOUT any parser fork.
"""

from __future__ import annotations

from pathlib import Path

from corrigenda.formats.alto.parser import build_document_manifest, parse_alto_file
from corrigenda.core.schemas import HyphenRole, PairingPolicy

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples"
_SAMPLE = _EXAMPLES / "sample.xml"


def _find_part1(pages) -> object | None:
    for page in pages:
        for lm in page.lines:
            if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH):
                return lm
    return None


def test_default_policy_reproduces_sequential_pairing():
    """Default build must find and link at least one hyphen pair in the
    sample — i.e. the historical behaviour is preserved."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    part1 = _find_part1(doc.pages)
    assert part1 is not None, "sample has a known hyphen pair"
    assert part1.hyphen_pair_line_id or part1.hyphen_forward_pair_id


def test_default_and_explicit_default_agree():
    """Passing PairingPolicy() explicitly must equal the implicit default."""
    a = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    b = build_document_manifest([(_SAMPLE, _SAMPLE.name)], PairingPolicy())
    a_roles = [lm.hyphen_role for p in a.pages for lm in p.lines]
    b_roles = [lm.hyphen_role for p in b.pages for lm in p.lines]
    assert a_roles == b_roles


def _linked_count(pages) -> int:
    return sum(
        1
        for p in pages
        for lm in p.lines
        if lm.hyphen_pair_line_id or lm.hyphen_forward_pair_id
    )


def test_realistic_gap_policy_keeps_adjacent_pairs():
    """A generous, realistic gap (a couple of line heights) must keep the
    sample's adjacent pairs linked — the policy only rejects outliers."""
    lenient = PairingPolicy(max_vertical_gap=500)
    pages, _ = parse_alto_file(_SAMPLE, _SAMPLE.name, pairing_policy=lenient)
    assert _linked_count(pages) > 0


def test_vertical_gap_skipped_across_pages():
    """VPOS restarts on every page, so max_vertical_gap must not be
    applied to a cross-page candidate (post-audit F7 fix)."""
    from corrigenda.core.schemas import Coords, LineManifest

    def _line(page: str, vpos: int) -> LineManifest:
        return LineManifest(
            line_id=f"L_{page}",
            page_id=page,
            block_id=f"B_{page}",
            line_order_global=0,
            line_order_in_block=0,
            coords=Coords(hpos=0, vpos=vpos, width=100, height=10),
            ocr_text="mot-",
        )

    policy = PairingPolicy(max_vertical_gap=5)
    bottom_of_p1 = _line("P1", vpos=900)  # bottom of page 1
    top_of_p2 = _line("P2", vpos=0)  # top of page 2 — VPOS not comparable
    assert policy.can_pair(bottom_of_p1, top_of_p2)

    # Intra-page, the same gap threshold does reject a distant candidate.
    far_same_page = _line("P1", vpos=2000)
    assert not policy.can_pair(bottom_of_p1, far_same_page)


def test_strict_gap_policy_breaks_every_pair():
    """A restrictive same-block-only + zero-gap policy must link fewer
    pairs than the default — proving the seam is honoured and not a
    no-op. (P2-5: the historical trick of a *negative* gap cap is now
    rejected by validation — see test below — so the strictest
    expressible policy is gap 0.)"""
    default_pages, _ = parse_alto_file(_SAMPLE, _SAMPLE.name)
    assert _linked_count(default_pages) > 0

    strict = PairingPolicy(max_vertical_gap=0)
    strict_pages, _ = parse_alto_file(_SAMPLE, _SAMPLE.name, pairing_policy=strict)
    assert _linked_count(strict_pages) < _linked_count(default_pages)


def test_negative_gap_cap_is_rejected_at_construction():
    """P2-5 — a negative vertical-gap cap is a nonsensical configuration
    (it can only reject-by-arithmetic-accident) and fails fast."""
    import pytest

    with pytest.raises(ValueError):
        PairingPolicy(max_vertical_gap=-1)
