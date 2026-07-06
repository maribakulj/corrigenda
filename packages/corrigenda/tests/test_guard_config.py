"""GuardConfig — the frozen, injectable guard-threshold policy (spec F13, §8.2).

Pins:
  - the policy is immutable (frozen);
  - ``policy_fingerprint()`` is stable, deterministic, and sensitive to
    any field change (it feeds the provenance ``processingStep``, §11);
  - the defaults reproduce the historical thresholds (byte-parity);
  - the config actually threads through the guards — a stricter config
    rejects a correction the default accepts, and vice versa.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from corrigenda.core.guards import check_line
from corrigenda.core.schemas import DEFAULT_GUARD_CONFIG, GuardConfig


def test_guard_config_is_frozen():
    cfg = GuardConfig()
    with pytest.raises(ValidationError):
        cfg.min_source_similarity = 0.9  # type: ignore[misc]


def test_default_matches_historical_constants():
    cfg = GuardConfig()
    # Stage C
    assert cfg.min_source_similarity == 0.35
    assert cfg.neighbour_margin == 0.15
    assert cfg.duplicate_threshold == 0.85
    assert cfg.duplicate_source_min_diff == 0.70
    assert cfg.absorption_length_ratio == 1.2
    assert cfg.absorption_concat_similarity == 0.8
    # Stage B
    assert cfg.part1_max_word_growth == 1
    assert cfg.part1_last_word_char_growth == 3
    assert cfg.part1_char_growth_ratio == 1.4
    assert cfg.part1_char_growth_slack == 8
    assert cfg.part2_collapse_ratio == 0.4
    # Stage A
    assert cfg.pair_drift_part1_word_growth == 2
    assert cfg.pair_drift_part2_collapse_ratio == 0.4


def test_fingerprint_is_stable_and_deterministic():
    a = GuardConfig().policy_fingerprint()
    b = GuardConfig().policy_fingerprint()
    assert a == b
    assert len(a) == 16
    assert DEFAULT_GUARD_CONFIG.policy_fingerprint() == a


def test_fingerprint_changes_when_a_field_changes():
    base = GuardConfig().policy_fingerprint()
    tuned = GuardConfig(min_source_similarity=0.5).policy_fingerprint()
    assert base != tuned


def test_stricter_source_similarity_threads_through_check_line():
    """A correction that the default accepts must be rejected under a
    config demanding higher source similarity — proving the config is
    honoured, not ignored."""
    source = "hello world"
    corrected = "hallo warld"  # similar but not identical
    default = check_line(source, corrected)
    strict = check_line(
        source, corrected, config=GuardConfig(min_source_similarity=0.99)
    )
    assert default.accepted
    assert not strict.accepted
    assert strict.reason == "too_different_from_source"
    assert strict.text == source  # falls back to OCR
