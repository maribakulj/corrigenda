"""ROADMAP V3 Phase 1 — the token aligner (core/alignment.py).

The shared component behind faithful slow-path projection, per-token
confidence and the future token_realign policy. Pure, deterministic,
monotonic; moves are FLAGGED, never acted on.
"""

from __future__ import annotations

from corrigenda.core.alignment import align_tokens, char_similarity


# ---------------------------------------------------------------------------
# char_similarity
# ---------------------------------------------------------------------------


def test_char_similarity_bounds():
    assert char_similarity("moindre", "moindre") == 1.0
    assert char_similarity("", "") == 1.0
    assert char_similarity("abc", "") == 0.0
    assert char_similarity("abc", "xyz") == 0.0


def test_char_similarity_ocr_confusion_is_high():
    # rn→m: the classic confusion keeps most characters.
    assert char_similarity("rnoindre", "moindre") > 0.7


# ---------------------------------------------------------------------------
# align_tokens — structure
# ---------------------------------------------------------------------------


def test_identity_alignment_is_perfect():
    al = align_tokens(["la", "grande", "porte"], ["la", "grande", "porte"])
    assert al.score == 1.0
    assert not al.move_suspected
    assert [(p.source_index, p.target_index) for p in al.pairs] == [
        (0, 0),
        (1, 1),
        (2, 2),
    ]


def test_insertion_keeps_neighbours_on_their_own_identity():
    """The case positional recycling gets wrong: an inserted word must
    NOT shift every following token onto the wrong source index."""
    al = align_tokens(["aaa", "bbb", "ccc"], ["aaa", "xyz", "bbb", "ccc"])
    assert al.source_for_target(0) == 0
    assert al.source_for_target(1) is None  # the insertion
    assert al.source_for_target(2) == 1
    assert al.source_for_target(3) == 2


def test_deletion_is_symmetrical():
    al = align_tokens(["aaa", "bbb", "ccc"], ["aaa", "ccc"])
    assert al.source_for_target(0) == 0
    assert al.source_for_target(1) == 2
    deleted = [p for p in al.pairs if p.target_index is None]
    assert [p.source_index for p in deleted] == [1]


def test_corrected_token_still_matches_its_source():
    al = align_tokens(
        ["la", "rnoindre", "choſe"], ["la", "moindre", "chose", "ajoutée"]
    )
    assert al.source_for_target(1) == 1  # rnoindre → moindre
    assert al.source_for_target(2) == 2  # choſe → chose
    assert al.source_for_target(3) is None  # ajoutée is new


def test_zero_evidence_tokens_do_not_match():
    """Identity must never ride a zero-similarity 'match': a fully
    replaced token falls to deletion + insertion."""
    al = align_tokens(["aaa"], ["zzz"])
    matched = [
        p for p in al.pairs if p.source_index is not None and p.target_index is not None
    ]
    assert matched == []
    assert al.score == 0.0


def test_empty_sequences():
    assert align_tokens([], []).pairs == ()
    al = align_tokens([], ["nouveau"])
    assert [(p.source_index, p.target_index) for p in al.pairs] == [(None, 0)]
    al = align_tokens(["perdu"], [])
    assert [(p.source_index, p.target_index) for p in al.pairs] == [(0, None)]


# ---------------------------------------------------------------------------
# Move suspicion — flagged, never acted on
# ---------------------------------------------------------------------------


def test_swapped_words_raise_the_move_flag():
    al = align_tokens(["grande", "porte", "ouverte"], ["porte", "grande", "ouverte"])
    assert al.move_suspected


def test_far_move_raises_the_flag():
    al = align_tokens(["premier", "mot", "dernier"], ["mot", "dernier", "premier"])
    assert al.move_suspected


def test_plain_correction_does_not_raise_the_flag():
    al = align_tokens(["la", "rnoindre", "chose"], ["la", "moindre", "chose"])
    assert not al.move_suspected


def test_determinism():
    a = align_tokens(["un", "deux", "trois"], ["un", "deux2", "trois", "quatre"])
    b = align_tokens(["un", "deux", "trois"], ["un", "deux2", "trois", "quatre"])
    assert a == b
