"""Stage-C boundary-migration guard: no word may migrate across a line seam.

The pair-level guards (Stage A/B) only run on lines the parser classified
as a hyphen pair. When the OCR mangles the end-of-line hyphen into some
other glyph (``re«`` instead of ``re-``), the line is never paired, so the
LLM is free to *complete the word by pulling its continuation up from the
next physical line* — a text migration the pair guards never see.

Real regression (BnF X0000002 through Mistral Small):

    565  source   "…trompettes re«"
    565  producer "…trompettes retentissent,"   <- pulled "tentissent" up
    566  source   "tentlssent, le roi monte sur la pla-"
    566  producer "le roi monte sur la place."   <- dropped "tentlssent,"

566 happened to be PART1 of the *next* pair (566/567), so Stage B reverted
it; 565 belonged to no pair and its absorption survived, yielding the
duplicated reading "…retentissent, tentlssent, le roi…". The invariant is
"no text migrates between physical lines" — it must hold regardless of
hyphen role, so the guard keys on the boundary tokens, not on detection.
"""

from __future__ import annotations

from corrigenda.core.guards import check_boundary_migration


def test_forward_absorption_across_mangled_hyphen_reverts_both():
    """565's corrected last word == source_last ⊕ next_first → both revert."""
    reverts = check_boundary_migration(
        [
            (
                "a",
                "Les fanfares de trompettes re«",
                "Les fanfares de trompettes retentissent,",
            ),
            ("b", "tentlssent, le roi monte sur la pla-", "le roi monte sur la place."),
        ]
    )
    assert set(reverts) == {"a", "b"}


def test_backward_absorption_reverts_both():
    """The next line's corrected FIRST word absorbed the previous line's tail."""
    reverts = check_boundary_migration(
        [
            ("a", "le chemin est absolu*", "le chemin est absolu"),
            ("b", "ment impraticable", "absolument impraticable"),
        ]
    )
    assert set(reverts) == {"a", "b"}


def test_clean_adjacent_corrections_not_flagged():
    """Two ordinary in-line corrections must NOT be reverted."""
    reverts = check_boundary_migration(
        [
            (
                "a",
                "prendre par le plus long omme l'on dit",
                "prendre par le plus long comme l'on dit",
            ),
            ("b", "ai laissé une bonüe moitié", "ai laissé une bonne moitié"),
        ]
    )
    assert reverts == {}


def test_legitimate_part1_hyphen_fragment_not_flagged():
    """A PART1 line whose fragment is corrected in place (préve → préve-)
    keeps its own tail — nothing crossed the seam, so it must survive."""
    reverts = check_boundary_migration(
        [
            ("a", "Nous le préve-", "Nous le préve-"),
            ("b", "nons qu'en atténuant", "nons qu'en atténuant"),
        ]
    )
    assert reverts == {}


def test_identity_lines_not_flagged():
    reverts = check_boundary_migration(
        [
            ("a", "une ligne stable", "une ligne stable"),
            ("b", "une autre ligne", "une autre ligne"),
        ]
    )
    assert reverts == {}


def test_empty_and_singleton_segments_are_safe():
    assert check_boundary_migration([]) == {}
    assert (
        check_boundary_migration([("a", "seule ligne", "seule ligne corrigée")]) == {}
    )
