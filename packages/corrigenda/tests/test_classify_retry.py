"""Direct unit tests for ``_classify_retry`` — pure pipeline classifier.

The classifier was extracted from ``_call_with_retry`` precisely so it
could be tested without spinning up a full pipeline, a chunk, an
observer, and a mock provider (see ``test_orchestrator.py`` for the
integration-level pins). Audit F2 flagged that nothing actually
exercised the function directly: every retry test went through the
orchestrator's retry loop, so a refactor of the classifier could
preserve end-to-end behavior on the sample fixture while quietly
breaking an edge case the fixture doesn't reach.

What this file pins:

  - Each of the 4 branches (hyphen-first, transient, llm-output,
    non-retryable) maps to the documented backoff/tag combination.
  - The 2nd-hyphen-violation path: when ``hyphen_already_seen=True``,
    ``HyphenIntegrityError`` falls through to ``is_llm_output_error``
    (linear backoff) because ``HyphenIntegrityError`` subclasses
    ``ValueError``. Without this latch the LLM would loop on
    zero-backoff retries.
  - The ``error_tag`` truncation contract (≤ 120 chars) on the three
    non-hyphen branches.
"""

from __future__ import annotations

import json

import pytest
from corrigenda.core.pipeline import _classify_retry, _RetryDecision
from corrigenda.core.validator import HyphenIntegrityError
from corrigenda.core.protocols import ProviderTransientError


# ---------------------------------------------------------------------------
# Branch 1 — Hyphen-violation, first occurrence
# ---------------------------------------------------------------------------


def test_first_hyphen_violation_zero_backoff_fixed_tag():
    """First HyphenIntegrityError per chunk: retry immediately at temp=0,
    tag with the fixed sentinel so observers can discriminate."""
    decision = _classify_retry(
        exc=HyphenIntegrityError("hyphen_integrity_violation: PART2 collapsed"),
        sanitised_msg="hyphen_integrity_violation: PART2 collapsed",
        attempt=1,
        hyphen_already_seen=False,
    )
    assert decision == _RetryDecision(
        is_retryable=True,
        backoff=0,
        error_tag="hyphen_integrity_violation",
        is_hyphen_violation=True,
    )


def test_first_hyphen_violation_ignores_attempt_number():
    """Hyphen backoff is fixed at 0 regardless of attempt — there is
    only ever ONE hyphen-special retry per chunk (the latch). Asserting
    attempt-independence prevents a future refactor from sneaking in
    a ramp."""
    for attempt in (1, 2, 3):
        decision = _classify_retry(
            exc=HyphenIntegrityError("hyp"),
            sanitised_msg="hyp",
            attempt=attempt,
            hyphen_already_seen=False,
        )
        assert decision.backoff == 0
        assert decision.is_hyphen_violation is True


# ---------------------------------------------------------------------------
# Branch 2 — Second hyphen violation falls into llm_output_error
# ---------------------------------------------------------------------------


def test_second_hyphen_violation_uses_linear_backoff_not_zero():
    """The per-chunk latch (``hyphen_already_seen=True``) exempts the
    1st occurrence only. Subsequent HyphenIntegrityErrors are treated
    like any other malformed LLM output: linear backoff (attempt seconds),
    sanitised tag, no hyphen flag. ``HyphenIntegrityError`` subclasses
    ``ValueError`` so the ``is_llm_output_error`` branch matches.

    Without this fallthrough behavior a deterministic prompt that always
    triggers a hyphen violation would loop on zero-backoff retries until
    max_attempts — burning quota in a tight loop."""
    decision = _classify_retry(
        exc=HyphenIntegrityError("hyphen_integrity_violation: again"),
        sanitised_msg="hyphen_integrity_violation: again",
        attempt=2,
        hyphen_already_seen=True,
    )
    assert decision.is_retryable is True
    assert decision.backoff == 2  # linear: attempt
    assert decision.is_hyphen_violation is False
    # Tag is the sanitised message, NOT the fixed sentinel — observers
    # discriminate "first hyphen retry" (sentinel) from "subsequent" (msg).
    assert decision.error_tag == "hyphen_integrity_violation: again"


# ---------------------------------------------------------------------------
# Branch 3 — Transient HTTP, exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempt,expected_backoff", [(1, 2), (2, 4), (3, 6)])
def test_transient_http_exponential_backoff(attempt, expected_backoff):
    """ProviderTransientError → backoff = attempt * 2. The 2x multiplier
    gives the upstream room to heal between attempts (5xx blip, 429,
    network failure). A bug that swapped this with linear backoff would
    halve recovery time on persistent transients."""
    decision = _classify_retry(
        exc=ProviderTransientError("upstream 503"),
        sanitised_msg="upstream 503",
        attempt=attempt,
        hyphen_already_seen=False,
    )
    assert decision == _RetryDecision(
        is_retryable=True,
        backoff=expected_backoff,
        error_tag="upstream 503",
        is_hyphen_violation=False,
    )


# ---------------------------------------------------------------------------
# Branch 4 — LLM output error (ValueError / JSONDecodeError), linear backoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempt,expected_backoff", [(1, 1), (2, 2), (3, 3)])
def test_llm_output_value_error_linear_backoff(attempt, expected_backoff):
    """Generic ValueError (validator rejected the JSON shape, missing
    key, etc.) → backoff = attempt. Stochastic one-off mistakes don't
    need exponential backoff — there's no upstream to heal."""
    decision = _classify_retry(
        exc=ValueError("missing 'lines' key"),
        sanitised_msg="missing 'lines' key",
        attempt=attempt,
        hyphen_already_seen=False,
    )
    assert decision == _RetryDecision(
        is_retryable=True,
        backoff=expected_backoff,
        error_tag="missing 'lines' key",
        is_hyphen_violation=False,
    )


def test_llm_output_json_decode_error_linear_backoff():
    """``json.JSONDecodeError`` is the other branch of the
    ``is_llm_output_error`` isinstance tuple. Same backoff curve as
    ValueError, distinct test so a future split of the two cases is
    caught."""
    exc = json.JSONDecodeError("Expecting value", "garbled output", 0)
    decision = _classify_retry(
        exc=exc,
        sanitised_msg="Expecting value: line 1 column 1 (char 0)",
        attempt=2,
        hyphen_already_seen=False,
    )
    assert decision.is_retryable is True
    assert decision.backoff == 2
    assert decision.is_hyphen_violation is False


# ---------------------------------------------------------------------------
# Branch 5 — Non-retryable: anything else (raw HTTPStatusError, OSError, …)
# ---------------------------------------------------------------------------


class _RawHttpStatusErrorLike(Exception):
    """Stub mimicking the raw ``httpx.HTTPStatusError`` a provider's
    wrapper deliberately leaks on a 4xx-non-429. Defined locally so
    corrigenda stays http-library-agnostic — the contract under test is
    "any non-matching exception → non-retryable", and a real
    ``HTTPStatusError`` doesn't match any of the four isinstance checks
    either. The full backend-side wiring (with the real httpx exception)
    is pinned by
    ``test_pipeline_classifies_client_http_4xx_as_non_retryable``.
    """


def test_raw_http_status_error_like_is_non_retryable():
    """A raw HTTP-status-like exception is what providers raise on 4xx-
    non-429: the wrapping helper deliberately doesn't convert it to
    ProviderTransientError (bad keys / wrong models don't heal). The
    classifier must mark it non-retryable so the chunk falls back
    immediately (no wasted attempts)."""
    decision = _classify_retry(
        exc=_RawHttpStatusErrorLike("401 Unauthorized"),
        sanitised_msg="401 Unauthorized",
        attempt=1,
        hyphen_already_seen=False,
    )
    assert decision == _RetryDecision(
        is_retryable=False,
        backoff=0,
        error_tag="401 Unauthorized",
        is_hyphen_violation=False,
    )


def test_runtime_error_non_retryable():
    """A bare RuntimeError (anything outside the three known retry classes)
    is non-retryable. The default-deny posture means new exception types
    surface as fallbacks rather than silently retrying forever."""
    decision = _classify_retry(
        exc=RuntimeError("unexpected"),
        sanitised_msg="unexpected",
        attempt=1,
        hyphen_already_seen=False,
    )
    assert decision.is_retryable is False
    assert decision.backoff == 0
    assert decision.is_hyphen_violation is False


def test_os_error_non_retryable():
    """Disk-on-fire (OSError) is NOT retryable here — it's caught one
    level up by the chunk_error safety net (see
    ``test_chunk_error_event_payload_shape``). Pinning here prevents a
    classifier change from accidentally swallowing OSErrors into retry."""
    decision = _classify_retry(
        exc=OSError("disk on fire"),
        sanitised_msg="disk on fire",
        attempt=1,
        hyphen_already_seen=False,
    )
    assert decision.is_retryable is False


# ---------------------------------------------------------------------------
# error_tag truncation — applies to the three non-hyphen branches
# ---------------------------------------------------------------------------


def test_error_tag_truncated_to_120_chars_on_transient():
    """Long messages from upstream are truncated at 120 chars before
    being put into the retry event payload. The SSE wire shouldn't
    carry kilobyte stack traces."""
    long_msg = "x" * 500
    decision = _classify_retry(
        exc=ProviderTransientError(long_msg),
        sanitised_msg=long_msg,
        attempt=1,
        hyphen_already_seen=False,
    )
    assert len(decision.error_tag) == 120
    assert decision.error_tag == "x" * 120


def test_error_tag_truncated_to_120_chars_on_llm_output_error():
    """Same truncation rule on the ValueError branch."""
    long_msg = "y" * 300
    decision = _classify_retry(
        exc=ValueError(long_msg),
        sanitised_msg=long_msg,
        attempt=1,
        hyphen_already_seen=False,
    )
    assert len(decision.error_tag) == 120


def test_error_tag_truncated_to_120_chars_on_non_retryable():
    """Truncation also applies on the non-retryable branch — the warning
    event consumed by observers carries this tag."""
    long_msg = "z" * 300
    decision = _classify_retry(
        exc=RuntimeError(long_msg),
        sanitised_msg=long_msg,
        attempt=1,
        hyphen_already_seen=False,
    )
    assert len(decision.error_tag) == 120


def test_first_hyphen_tag_is_fixed_sentinel_not_truncated_message():
    """On the hyphen-first branch the tag is the fixed sentinel
    ``"hyphen_integrity_violation"``, NOT the truncated message —
    even if the sanitised message starts with the same string.
    Observers depend on this exact constant for level routing."""
    long_hyp_msg = "hyphen_integrity_violation: " + ("x" * 300)
    decision = _classify_retry(
        exc=HyphenIntegrityError(long_hyp_msg),
        sanitised_msg=long_hyp_msg,
        attempt=1,
        hyphen_already_seen=False,
    )
    assert decision.error_tag == "hyphen_integrity_violation"
    # Specifically NOT the truncated message form.
    assert "x" not in decision.error_tag


# ---------------------------------------------------------------------------
# Decision dataclass invariants
# ---------------------------------------------------------------------------


def test_retry_decision_is_frozen():
    """``_RetryDecision`` is a frozen dataclass — once classified, the
    retry loop can't mutate the decision (paranoid invariant)."""
    decision = _classify_retry(
        exc=ValueError("x"),
        sanitised_msg="x",
        attempt=1,
        hyphen_already_seen=False,
    )
    with pytest.raises((AttributeError, Exception)):
        decision.backoff = 999  # type: ignore[misc]
