"""Plan V2.2 — per-job cooperative cancellation registry.

The library's pipeline already accepts a ``should_abort`` probe (polled
between pages and chunks; raises ``CorrectionAborted`` with no output
written). This registry is the backend-side source of those probes: one
``asyncio.Event`` per active job, registered at creation, discarded when
the run settles. The cancel endpoint sets the event; the runner's probe
reads it.

In-process by design, like the JobStore it accompanies: cancellation of
a job requires reaching the worker that runs it (single-worker
deployment — see the Dockerfile note on ``--workers 1``).
"""

from __future__ import annotations

import asyncio


class CancellationRegistry:
    """One cancellation event per active job."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def register(self, job_id: str) -> asyncio.Event:
        """Create (or return) the cancellation event for ``job_id``."""
        event = self._events.get(job_id)
        if event is None:
            event = asyncio.Event()
            self._events[job_id] = event
        return event

    def request(self, job_id: str) -> bool:
        """Set the event. Idempotent. False when the job is unknown
        (already settled and discarded, or never registered)."""
        event = self._events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def is_requested(self, job_id: str) -> bool:
        event = self._events.get(job_id)
        return event is not None and event.is_set()

    def discard(self, job_id: str) -> None:
        """Drop the entry once the run settled (any terminal state)."""
        self._events.pop(job_id, None)
