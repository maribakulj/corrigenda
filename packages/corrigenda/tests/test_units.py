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
