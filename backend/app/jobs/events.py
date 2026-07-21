"""Server-side job event vocabulary (P3.6).

The engine's :class:`~corrigenda.core.schemas.PipelineEventType` names
only what the PIPELINE can emit; everything the SERVER says about a job
— its lifecycle, the frontend's initial state, the SSE transport
signals — lives here. The string values ride the same SSE wire as the
engine's and are pinned by ``tests/test_sse_event_contract.py``; they
stay stable across releases.
"""

from __future__ import annotations

from enum import Enum


class JobEventType(str, Enum):
    """Job lifecycle + transport events emitted by the backend."""

    # Job lifecycle (emitted by JobRunner)
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Cooperative cancellation (``should_abort`` probe tripped): the
    #: run raised ``CorrectionAborted``, no output was promoted.
    #: Terminal, like ``completed``/``failed``.
    CANCELLED = "cancelled"

    # Frontend-only initial state (listed so the contract test can
    # verify the frontend list against the canonical union).
    QUEUED = "queued"

    # Transport-layer events (emitted by JobStore.stream_events).
    KEEPALIVE = "keepalive"
    ERROR = "error"


__all__ = ["JobEventType"]
