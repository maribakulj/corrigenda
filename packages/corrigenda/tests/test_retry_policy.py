"""RetryPolicy — injectable retry ramp / attempt cap / budget (spec F9).

Pins:
  - default() reproduces the historical ramp (0.0, 0.3, 0.5), cap 3;
  - deterministic() makes every attempt temperature 0.0;
  - temperature_for clamps beyond the ramp length;
  - the policy threads into _classify_retry's backoff computation.
"""

from __future__ import annotations

import pytest

from corrigenda.pipeline.correction_pipeline import _classify_retry
from corrigenda.protocols.provider import ProviderTransientError
from corrigenda.schemas import DEFAULT_RETRY_POLICY, RetryPolicy


def test_default_reproduces_historical_ramp():
    p = RetryPolicy.default()
    assert p.max_attempts == 3
    assert p.temperatures == (0.0, 0.3, 0.5)
    assert p.per_chunk_budget == 6
    assert [p.temperature_for(n) for n in (1, 2, 3)] == [0.0, 0.3, 0.5]
    assert DEFAULT_RETRY_POLICY == RetryPolicy.default()


def test_deterministic_is_all_zero():
    p = RetryPolicy.deterministic()
    assert all(p.temperature_for(n) == 0.0 for n in range(1, 6))
    assert p.max_attempts == 3  # only the temperatures change


def test_temperature_for_clamps_to_last_entry():
    p = RetryPolicy(temperatures=(0.0, 0.5))
    assert p.temperature_for(1) == 0.0
    assert p.temperature_for(2) == 0.5
    assert p.temperature_for(3) == 0.5  # clamped
    assert p.temperature_for(99) == 0.5


def test_is_frozen():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RetryPolicy().max_attempts = 9  # type: ignore[misc]


def test_default_backoff_matches_historical_curve():
    """Transient HTTP → attempt * 2; malformed output → attempt * 1."""
    for attempt, expected in [(1, 2), (2, 4), (3, 6)]:
        d = _classify_retry(
            exc=ProviderTransientError("boom"),
            sanitised_msg="boom",
            attempt=attempt,
            hyphen_already_seen=False,
        )
        assert d.backoff == expected

    for attempt, expected in [(1, 1), (2, 2)]:
        d = _classify_retry(
            exc=ValueError("bad json"),
            sanitised_msg="bad json",
            attempt=attempt,
            hyphen_already_seen=False,
        )
        assert d.backoff == expected


def test_custom_backoff_base_threads_through_classifier():
    slow = RetryPolicy(transient_backoff_base=5.0)
    d = _classify_retry(
        exc=ProviderTransientError("boom"),
        sanitised_msg="boom",
        attempt=2,
        hyphen_already_seen=False,
        policy=slow,
    )
    assert d.backoff == 10.0
