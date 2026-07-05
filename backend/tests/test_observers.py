"""Tests for backend observers (CompositeObserver + LoggingObserver).

Roadmap L4 — backend/app/jobs/observers.py had zero direct test
coverage. These tests pin the two non-obvious contracts of the
adapter layer:

  - CompositeObserver isolates failures: one broken observer must not
    deny event delivery to siblings.
  - LoggingObserver's level mapping (warning vs debug) — covered in
    L8 (T1a), not here.
"""

from __future__ import annotations

import logging
from typing import Any

from app.jobs.observers import CompositeObserver, LoggingObserver


class _Recorder:
    """Captures every (event_type, payload) pair it receives."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


class _Boom:
    """Always raises — simulates a misbehaving downstream observer."""

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        raise RuntimeError(f"observer broken on {event_type!r}")


def test_composite_observer_isolates_failing_observer(caplog):
    """Roadmap L4 (T0c) — a crashing observer must not silence the next.

    The fan-out pattern is the whole point of CompositeObserver: the
    JobStore observer feeds the SSE stream, the LoggingObserver feeds
    operators. A bug in either must not block the other. The audit
    flagged this as a 0-test contract.
    """
    boom = _Boom()
    recorder = _Recorder()
    composite = CompositeObserver([boom, recorder])

    with caplog.at_level(logging.ERROR, logger="app.jobs.observers"):
        composite.on_event("page_started", {"page_id": "P1"})

    # Delivery to the well-behaved observer is the critical invariant.
    assert recorder.events == [("page_started", {"page_id": "P1"})]

    # The failure was surfaced through the standard logger (not silently
    # swallowed) so an operator can spot a sick observer.
    matching = [r for r in caplog.records if "raised on event" in r.message]
    assert matching, "CompositeObserver swallowed the failure without logging"
    # And the exception info travelled with the log record.
    assert any(r.exc_info is not None for r in matching), (
        "Log record carries no exc_info — the traceback would be lost"
    )


def test_composite_observer_with_no_observers_is_a_noop():
    """Defensive: an empty observer list must not error on every event."""
    composite = CompositeObserver([])
    # Just must not raise.
    composite.on_event("anything", {})


def test_composite_observer_calls_observers_in_registration_order():
    """Order matters for the JobStore-first / Logger-second pattern used
    by JobRunner: SSE clients see the event before it lands in the log."""
    calls: list[str] = []

    class _Tagged:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
            calls.append(self._tag)

    composite = CompositeObserver([_Tagged("first"), _Tagged("second"), _Tagged("third")])
    composite.on_event("foo", {})

    assert calls == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# LoggingObserver — level mapping per event type (roadmap L8 / T1a)
# ---------------------------------------------------------------------------


def test_logging_observer_routes_warning_events_to_warning_level(caplog):
    """Roadmap L8 (T1a) — the three event types in `_WARNING_EVENTS`
    (`warning`, `chunk_error`, `hyphen_partner_missing`) must surface at
    WARNING level so operators alerting on `level=WARNING` catch them.

    Pre-L8 this mapping was entirely untested: a refactor that moved
    a critical event out of `_WARNING_EVENTS` (silently dropping it to
    DEBUG) would not have failed any test.

    The assertion is filtered to the `corrigenda.pipeline` logger and
    keyed on event-type substrings rather than a strict count, so an
    unrelated module emitting its own WARNING during the test cannot
    flip the result. The contract we pin is: for each of these three
    event types, the OBSERVER produced at least one WARNING record on
    the corrigenda.pipeline logger.
    """
    observer = LoggingObserver()

    with caplog.at_level(logging.DEBUG, logger="corrigenda.pipeline"):
        observer.on_event("warning", {"chunk_id": "c1", "message": "fallback"})
        observer.on_event("chunk_error", {"chunk_id": "c1", "exception_type": "OSError"})
        observer.on_event("hyphen_partner_missing", {"line_id": "L1", "direction": "backward"})

    warning_records = [
        r for r in caplog.records if r.levelname == "WARNING" and r.name == "corrigenda.pipeline"
    ]
    messages = " ".join(r.message for r in warning_records)
    for event_type in ("warning", "chunk_error", "hyphen_partner_missing"):
        assert event_type in messages, (
            f"event {event_type!r} did not produce a WARNING-level log "
            f"record on corrigenda.pipeline; got messages: {messages!r}"
        )


def test_logging_observer_routes_lifecycle_events_to_debug_level(caplog):
    """Roadmap L8 (T1a) — non-warning events stream at DEBUG so production
    logs (typically level=INFO) aren't drowned by per-chunk noise.

    Pre-L8 there was no test that a `chunk_completed` or `page_started`
    event NOT surface at WARNING. A future maintainer adding a new
    event type would have to read `_WARNING_EVENTS` to know the default;
    this test pins the contract.

    The DEBUG-count check is filtered to the `corrigenda.pipeline`
    logger and asserts presence-by-event-type rather than a strict
    count, so the test stays green if an unrelated logger emits a
    same-level record during the run.
    """
    observer = LoggingObserver()

    lifecycle_events = (
        "page_started",
        "chunk_planned",
        "chunk_started",
        "chunk_completed",
        "page_completed",
        "retry",
    )
    with caplog.at_level(logging.DEBUG, logger="corrigenda.pipeline"):
        observer.on_event("page_started", {"page_id": "P1"})
        observer.on_event("chunk_planned", {"page_id": "P1", "chunk_count": 3})
        observer.on_event("chunk_started", {"chunk_id": "c1"})
        observer.on_event("chunk_completed", {"chunk_id": "c1"})
        observer.on_event("page_completed", {"page_id": "P1"})
        observer.on_event("retry", {"chunk_id": "c1", "attempt": 1})

    # The OBSERVER must not emit any WARNING on its own logger for
    # these events (other modules' WARNINGs are out of scope).
    warning_records = [
        r for r in caplog.records if r.levelname == "WARNING" and r.name == "corrigenda.pipeline"
    ]
    assert warning_records == [], (
        f"lifecycle events leaked to WARNING on corrigenda.pipeline: "
        f"{[(r.levelname, r.message) for r in warning_records]}"
    )
    debug_messages = " ".join(
        r.message
        for r in caplog.records
        if r.levelname == "DEBUG" and r.name == "corrigenda.pipeline"
    )
    for event_type in lifecycle_events:
        assert event_type in debug_messages, (
            f"lifecycle event {event_type!r} did not produce a DEBUG-level "
            f"log record on corrigenda.pipeline; got messages: {debug_messages!r}"
        )
