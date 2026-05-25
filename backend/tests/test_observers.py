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

from app.jobs.observers import CompositeObserver


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
