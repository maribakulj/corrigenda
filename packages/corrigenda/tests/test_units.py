"""ADR-010 (first slice) — ONE derivation of hyphen groups.

Cross-validated against the rich generator: every structure the
generator encodes (pair, chain, cross-page seam) must surface as
exactly one group with the right members in the right order — and
nothing else may group.
"""

from __future__ import annotations

from hypothesis import given, settings

from corrigenda.core.identity import LineRef
from corrigenda.core.units import derive_hyphen_groups, hyphen_group_by_line
from corrigenda.formats.alto.parser import build_document_manifest

from tests._alto_gen import rich_alto_documents
from tests.test_properties_hypothesis import _write_tmp


def _all_lines(path):
    doc = build_document_manifest([(path, path.name)])
    return [lm for page in doc.pages for lm in page.lines]


@settings(max_examples=40, deadline=None)
@given(doc_and_roles=rich_alto_documents())
def test_groups_match_the_generated_structures(doc_and_roles) -> None:
    doc, expected = doc_and_roles
    path = _write_tmp(doc)
    try:
        lines = _all_lines(path)
        groups = derive_hyphen_groups(lines)
        by_line = hyphen_group_by_line(groups)
        role_of = expected  # line_id → generated role
        ref_of = {
            lm.line_id: LineRef(page_id=lm.page_id, line_id=lm.line_id) for lm in lines
        }

        # 1. Membership is exactly the non-plain lines.
        grouped_ids = {ref.line_id for ref in by_line}
        expected_ids = {lid for lid, role in role_of.items() if role != "plain"}
        assert grouped_ids == expected_ids

        # 2. Every group is one generated structure, members in reading
        #    order: pair = 2 members, chain = 3 (PART1, BOTH, PART2),
        #    seam = 2 members spanning both pages.
        for group in groups:
            roles = [role_of[m.line_id] for m in group.members]
            assert roles in (
                ["part1", "part2"],
                ["part1", "both", "part2"],
                ["seam1", "seam2"],
            ), f"unexpected group shape: {roles}"
            assert group.spans_pages == (roles == ["seam1", "seam2"])
            # The generator emits only explicit SUBS hyphenation.
            assert group.explicit

        # 3. Groups partition their members (no line in two groups).
        seen: set[LineRef] = set()
        for group in groups:
            for member in group.members:
                assert member not in seen
                seen.add(member)
        assert seen == {ref_of[lid] for lid in expected_ids}
    finally:
        path.unlink(missing_ok=True)


@settings(max_examples=25, deadline=None)
@given(doc_and_roles=rich_alto_documents())
def test_page_local_derivation_drops_the_severed_seam(doc_and_roles) -> None:
    """A page-scoped consumer (the chunk planner) derives groups from ONE
    page's lines: a cross-page pair contributes only its on-page member,
    which must NOT form a group — the join is reconciled, never planned."""
    doc, expected = doc_and_roles
    path = _write_tmp(doc)
    try:
        manifest = build_document_manifest([(path, path.name)])
        for page in manifest.pages:
            for group in derive_hyphen_groups(page.lines):
                roles = [expected[m.line_id] for m in group.members]
                assert "seam1" not in roles and "seam2" not in roles, (
                    "a severed cross-page pair must not group page-locally"
                )
                assert not group.spans_pages
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# split_forward_link — the unit SPLIT operation (ADR-010, slice 2)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from corrigenda.core.schemas import HyphenRole, HyphenSplit  # noqa: E402
from corrigenda.core.units import split_forward_link  # noqa: E402

# PART1 → BOTH → PART2 chain: 'porte' split over L0/L1, 'fondation' over
# L1/L2 — every role the split has to migrate, with explicit SUBS.
_CHAIN_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"><Layout>
<Page ID="P1" WIDTH="1000" HEIGHT="1000">
<PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
<TextLine ID="L0" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20">
<String ID="S0" CONTENT="por" HPOS="10" VPOS="10" WIDTH="60" HEIGHT="20" \
SUBS_TYPE="HypPart1" SUBS_CONTENT="porte"/><HYP CONTENT="-"/></TextLine>
<TextLine ID="L1" HPOS="10" VPOS="40" WIDTH="900" HEIGHT="20">
<String ID="S1" CONTENT="te" HPOS="10" VPOS="40" WIDTH="40" HEIGHT="20" \
SUBS_TYPE="HypPart2" SUBS_CONTENT="porte"/>
<String ID="S2" CONTENT="fon" HPOS="60" VPOS="40" WIDTH="60" HEIGHT="20" \
SUBS_TYPE="HypPart1" SUBS_CONTENT="fondation"/><HYP CONTENT="-"/></TextLine>
<TextLine ID="L2" HPOS="10" VPOS="70" WIDTH="900" HEIGHT="20">
<String ID="S3" CONTENT="dation" HPOS="10" VPOS="70" WIDTH="90" HEIGHT="20" \
SUBS_TYPE="HypPart2" SUBS_CONTENT="fondation"/></TextLine>
</TextBlock></PrintSpace></Page></Layout></alto>"""


def _chain_lines():
    path = _write_tmp(_CHAIN_ALTO)
    try:
        return _all_lines(path)
    finally:
        path.unlink(missing_ok=True)


def test_split_severs_at_the_both_tail() -> None:
    """Cut L1→L2: the BOTH tail keeps its backward pair (it is still
    PART2 of 'porte'), the PART2 head becomes a plain line."""
    lines = _chain_lines()
    l0, l1, l2 = lines
    record = split_forward_link(l1, l2)
    assert record == HyphenSplit(page_id="P1", tail_line_id="L1", head_line_id="L2")
    assert l1.hyphen_role is HyphenRole.PART2
    assert l1.hyphen_pair_line_id == "L0"
    assert l1.hyphen_subs_content == "porte"
    assert l1.hyphen_forward_pair_id is None
    assert l1.hyphen_forward_subs_content is None
    assert l2.hyphen_role is HyphenRole.NONE
    assert l2.hyphen_pair_line_id is None
    assert l2.hyphen_subs_content is None
    assert [lm.ocr_text for lm in lines] == ["por-", "tefon-", "dation"]
    groups = derive_hyphen_groups(lines)
    assert [[m.line_id for m in g.members] for g in groups] == [["L0", "L1"]]


def test_split_severs_at_the_both_head() -> None:
    """Cut L0→L1: the BOTH head becomes PART1 of ITS OWN forward word —
    the forward link/subs migrate into the plain pair fields, where
    PART1 carries them."""
    lines = _chain_lines()
    l0, l1, l2 = lines
    record = split_forward_link(l0, l1)
    assert record == HyphenSplit(page_id="P1", tail_line_id="L0", head_line_id="L1")
    assert l0.hyphen_role is HyphenRole.NONE
    assert l0.hyphen_pair_line_id is None
    assert l0.hyphen_subs_content is None
    assert l1.hyphen_role is HyphenRole.PART1
    assert l1.hyphen_pair_line_id == "L2"
    assert l1.hyphen_subs_content == "fondation"
    assert l1.hyphen_source_explicit
    assert l1.hyphen_forward_pair_id is None
    groups = derive_hyphen_groups(lines)
    assert [[m.line_id for m in g.members] for g in groups] == [["L1", "L2"]]
    assert groups[0].explicit


def test_split_of_a_plain_pair_clears_both_sides() -> None:
    """After the chain is fully severed no group remains and every line
    is plain — the conservative degenerate end state."""
    lines = _chain_lines()
    l0, l1, l2 = lines
    split_forward_link(l0, l1)
    split_forward_link(l1, l2)  # now a plain PART1→PART2 pair
    assert [lm.hyphen_role for lm in lines] == [
        HyphenRole.NONE,
        HyphenRole.NONE,
        HyphenRole.NONE,
    ]
    assert derive_hyphen_groups(lines) == ()


def test_split_refuses_an_absent_link() -> None:
    """Severing a link that is not there is an engine bug, not a no-op."""
    lines = _chain_lines()
    l0, l1, l2 = lines
    with pytest.raises(RuntimeError, match="does not continue onto"):
        split_forward_link(l0, l2)
