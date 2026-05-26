"""Backend-side `PipelineObserver` implementations.

ARCHITECTURE.md ADR-006 keeps `alto_core.pipeline.correction_pipeline`
free of `logging` — the pipeline emits structured events via
:class:`PipelineObserver`, and host applications decide where they go.

Two ready-made observers ship here:

- :class:`LoggingObserver` — routes events to the standard ``logging``
  module so backend operators see them in their configured handlers.
- :class:`CompositeObserver` — fans events out to multiple observers in
  order (job-store fan-out + logging, typically).
"""

from __future__ import annotations

import logging
from typing import Any

from app.protocols import PipelineObserver

logger = logging.getLogger("alto_core.pipeline")

# Event names the pipeline emits that warrant a non-debug log level.
# Anything else is treated as informational (debug).
_WARNING_EVENTS = frozenset({"warning", "chunk_error", "hyphen_partner_missing"})


class LoggingObserver:
    """Forward pipeline events to the stdlib ``logging`` module.

    Maps a small set of event types to ``warning`` level; everything
    else lands at ``debug`` so production logs aren't drowned by the
    normal lifecycle stream (page_started, chunk_completed, ...).
    """

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type in _WARNING_EVENTS:
            logger.warning("pipeline %s: %s", event_type, payload)
        else:
            logger.debug("pipeline %s: %s", event_type, payload)


class CompositeObserver:
    """Fan an event out to several observers (e.g. JobStore + logging).

    Each observer is called in order; an exception in one does not stop
    delivery to the next — we surface it via the standard logger so a
    misbehaving observer can't silently kill the pipeline.
    """

    def __init__(self, observers: list[PipelineObserver]) -> None:
        self._observers = list(observers)

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        for obs in self._observers:
            try:
                obs.on_event(event_type, payload)
            except Exception:
                logger.exception("observer %r raised on event %r", type(obs).__name__, event_type)
