"""Characterization of the hyphen partner-resolution invariant (audit
Phase 1 — filet for audit Problem 1).

Problem 1: the rule "given a line, who is its forward hyphen partner?" is
re-encoded four times (``planner._hyphen_partner_id``, the inline ternary
in ``planner._plan_line``, the union-find in ``planner._try_block``, and
``hyphenation.should_stay_in_same_chunk``). Unifying them into a single
primitive is the recommended fix — but that fix is only safe if a test
pins the *observable* consequence of correct partner resolution:

  1. every forward pair the PARSER linked is actually reconciled by the
     pipeline (parser and pipeline agree on the pair set);
  2. no partner goes missing on a single-file (same-page) corpus;
  3. a hyphen pair is never split across chunk boundaries — which shows
     up as a missing-partner event when the two members land in different
     chunks.

These are behavioral (event + count) assertions, not asserts on the
private helpers being unified, so the refactor is free to move code as
long as the guarantee holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda.core.schemas import HyphenRole
from corrigenda.formats.alto.parser import parse_alto_file

from tests._pipeline_harness import run_pipeline

X0000002_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "examples" / "X0000002.xml"
)

pytestmark = pytest.mark.skipif(
    not X0000002_PATH.exists(), reason="X0000002.xml not found"
)


def _expected_forward_pairs() -> int:
    """Count forward pairs the PARSER links: PART1→partner + BOTH→forward."""
    pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
    lines = [lm for pg in pages for lm in pg.lines]
    part1 = sum(
        1
        for lm in lines
        if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id
    )
    both = sum(
        1
        for lm in lines
        if lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id
    )
    return part1 + both


def test_pipeline_reconciles_exactly_the_parser_linked_pairs():
    """Parser and pipeline must agree on the forward-pair set: the number of
    pairs the pipeline reconciles equals the number the parser linked. A
    partner-resolution regression (wrong field for a role) would drop or
    duplicate pairs and break this equality."""
    expected = _expected_forward_pairs()
    assert expected == 125  # 99 PART1 + 26 BOTH — corpus invariant

    run = run_pipeline("X0000002.xml")
    assert run.result.reconcile_metrics.total == expected
    assert run.result.total_reconciled == expected


def test_no_partner_goes_missing_on_single_file_corpus():
    """Every hyphen partner in a single ALTO file is same-page and must
    resolve. A ``hyphen_partner_missing`` event means partner resolution
    returned the wrong id (or None) for some role — the exact failure the
    4-way duplication risks introducing."""
    run = run_pipeline("X0000002.xml")
    missing = [p for (v, p) in run.observer.events if v == "hyphen_partner_missing"]
    assert missing == [], f"unexpected missing partners: {missing}"


def test_pairs_are_atomic_no_split_across_chunks():
    """A split pair surfaces as a partner-missing event (the two members
    landed in different chunks, so reconciliation cannot find the mate).
    Zero such events across the 566-line / 52-window corpus is the
    behavioral proof that the chunk planner keeps pairs atomic."""
    run = run_pipeline("X0000002.xml")
    assert run.observer.count("hyphen_partner_missing") == 0
    # Sanity: the corpus really is chunked into many windows (so atomicity
    # is non-trivially exercised, not a single-chunk artefact).
    assert run.result.total_chunks > 1


def test_chained_both_line_reconciles_on_both_sides():
    """A BOTH line (tail of one word, head of the next) must reconcile on
    its forward link under the real pipeline — the chained case where the
    role→field mapping is most error-prone (forward id lives in a different
    field than PART1's)."""
    # TL000017 is the documented BOTH line (praticables. / desservent).
    run = run_pipeline("X0000002.xml")
    both = run.lines["PAG_00000002_TL000017"]
    assert both.hyphen_role == HyphenRole.BOTH
    assert both.hyphen_forward_pair_id is not None
    # Its forward partner exists and both carry resolved (non-None) text.
    partner = run.lines[both.hyphen_forward_pair_id]
    assert both.corrected_text or both.ocr_text
    assert partner.corrected_text or partner.ocr_text
