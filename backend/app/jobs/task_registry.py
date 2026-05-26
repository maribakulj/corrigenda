"""Tracked background tasks with graceful shutdown.

``asyncio.create_task(coro)`` has two production-bad behaviours:

1. Python keeps only a weak reference to the resulting Task, so if no
   user code holds a strong reference, the GC can collect the Task
   while it's still running and silently kill the work.
2. On process shutdown (SIGTERM from Docker/HF Spaces), tasks are
   cancelled abruptly — output files left half-written, JobStore
   left in inconsistent state.

:class:`BackgroundTaskRegistry` fixes both: it keeps a strong reference
to every spawned task, logs unhandled exceptions through the standard
logger (so they reach the JSON pipeline set up in
``app.observability.logging_config``), and offers a ``shutdown()``
coroutine the FastAPI lifespan can ``await`` to drain in-flight tasks
or cancel them after a deadline.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTaskRegistry:
    """Strong-reference registry for fire-and-forget asyncio tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_started = False

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Schedule ``coro`` and track its Task.

        Raises ``RuntimeError`` if the registry has already started
        shutting down — refusing new work during shutdown is part of
        the graceful-stop contract.
        """
        if self._shutdown_started:
            raise RuntimeError("registry is shutting down; refusing new task")
        task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "background task %r crashed: %s",
                task.get_name(),
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    async def shutdown(self, *, timeout: float = 30.0) -> None:
        """Stop accepting new tasks, wait up to ``timeout`` seconds for
        in-flight tasks to finish, then cancel any stragglers.

        Idempotent — calling twice has the same effect as once. Logs
        a summary so the operator knows how many tasks completed
        cleanly vs were cancelled at the deadline.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True

        pending = set(self._tasks)
        if not pending:
            logger.info("BackgroundTaskRegistry shutdown: no in-flight tasks")
            return

        logger.info(
            "BackgroundTaskRegistry shutdown: waiting up to %.0fs for %d task(s)",
            timeout,
            len(pending),
        )
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            task.cancel()
        # Give cancelled tasks a chance to settle so we log their final state.
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)
            logger.warning(
                "BackgroundTaskRegistry shutdown: cancelled %d task(s) past deadline",
                len(still_pending),
            )
        logger.info(
            "BackgroundTaskRegistry shutdown: %d completed, %d cancelled",
            len(done),
            len(still_pending),
        )
