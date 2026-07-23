"""ROADMAP V3 Phase 3 — QE scoring + routing (core/quality.py).

The pre-LLM "does this line need correction?" signal and the Router
that turns it into skip/llm/escalate. Pure, deterministic, opt-in.
The load-bearing regression test: historical orthography (long-s,
u-for-v) must NOT read as "needs correction" — corrigenda preserves it.
"""

from __future__ import annotations

import pytest

from corrigenda import (
    HeuristicQEScorer,
    QEScorer,
    RoutingDecision,
    RoutingPolicy,
    route_line,
)


# ---------------------------------------------------------------------------
# HeuristicQEScorer — orthography-neutral signals only
# ---------------------------------------------------------------------------


def test_scorer_satisfies_the_protocol():
    assert isinstance(HeuristicQEScorer(), QEScorer)


def test_historical_orthography_is_not_flagged():
    """The finding this scorer exists to encode: a clean, human-corrected
    17th-c. line is FULL of long-s and u-for-v — it must score 0, not
    high. A naive archaic-glyph heuristic got this backwards."""
    scorer = HeuristicQEScorer()
    clean_historical = "qui les cultiuent; Et enfin qu'il eſt bon de les auoir"
    assert scorer.needs_correction(clean_historical) == 0.0


def test_digit_in_word_is_flagged_without_a_lexicon():
    """l/1 and o/0 confusions strand a digit inside a word — an
    orthography-neutral OCR-breakage signal that fires with no lexicon."""
    scorer = HeuristicQEScorer()
    assert scorer.needs_correction("dans la vil1e") > 0.0
    assert scorer.needs_correction("c0mme il faut") > 0.0
    # A bare number token is not a broken word.
    assert scorer.needs_correction("page 1789 du livre") == 0.0


def test_no_lexicon_is_sparse_by_design():
    """Without a lexicon the heuristic cannot judge historical OCR — it
    abstains (only the digit signal fires) rather than mislead."""
    scorer = HeuristicQEScorer()
    # Real OCR errors that are not digit-shaped go undetected here…
    assert scorer.needs_correction("qui les cukiuent eft bon") == 0.0


def test_lexicon_distinguishes_real_ocr_errors():
    """With a HISTORICAL lexicon, an out-of-vocabulary token (a real OCR
    non-word) is flagged while valid historical forms are not."""
    lexicon = {"qui", "les", "cultiuent", "et", "enfin", "bon", "eſt"}
    scorer = HeuristicQEScorer(lexicon=lexicon)
    # 'cukiuent' and 'eft' are OCR non-words; the rest are in the lexicon.
    score = scorer.needs_correction("qui les cukiuent eft bon")
    assert score == pytest.approx(2 / 5)


def test_empty_and_punctuation_only_score_zero():
    scorer = HeuristicQEScorer()
    assert scorer.needs_correction("") == 0.0
    assert scorer.needs_correction("  —  ;  ") == 0.0


# ---------------------------------------------------------------------------
# RoutingPolicy + route_line
# ---------------------------------------------------------------------------


def test_default_policy_routes_everything_to_llm():
    """Conservative default: routing disabled, historical behaviour."""
    policy = RoutingPolicy()
    for score in (0.0, 0.3, 0.7, 1.0):
        assert route_line(score, policy) == RoutingDecision.LLM


def test_skip_band_spares_clean_lines():
    policy = RoutingPolicy(skip_at_or_below=0.1)
    assert route_line(0.0, policy) == RoutingDecision.SKIP
    assert route_line(0.1, policy) == RoutingDecision.SKIP  # tie → skip
    assert route_line(0.11, policy) == RoutingDecision.LLM


def test_escalate_band_flags_the_riskiest():
    policy = RoutingPolicy(escalate_at_or_above=0.8)
    assert route_line(0.8, policy) == RoutingDecision.ESCALATE  # tie → escalate
    assert route_line(0.79, policy) == RoutingDecision.LLM


def test_full_three_band_routing():
    policy = RoutingPolicy(skip_at_or_below=0.1, escalate_at_or_above=0.8)
    assert route_line(0.05, policy) == RoutingDecision.SKIP
    assert route_line(0.5, policy) == RoutingDecision.LLM
    assert route_line(0.9, policy) == RoutingDecision.ESCALATE


def test_overlapping_bands_are_refused():
    with pytest.raises(ValueError, match="must be <"):
        RoutingPolicy(skip_at_or_below=0.6, escalate_at_or_above=0.5)
    with pytest.raises(ValueError, match="must be <"):
        RoutingPolicy(skip_at_or_below=0.5, escalate_at_or_above=0.5)


def test_policy_is_frozen_and_fingerprintable():
    policy = RoutingPolicy(skip_at_or_below=0.1)
    assert len(policy.policy_fingerprint()) == 16
    assert policy.policy_fingerprint() != RoutingPolicy().policy_fingerprint()
    with pytest.raises((TypeError, ValueError)):
        policy.skip_at_or_below = 0.2  # type: ignore[misc]
