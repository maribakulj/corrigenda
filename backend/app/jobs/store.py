"""In-memory job store with SSE fan-out and TTL eviction.

All state-mutating methods are guarded by an internal re-entrant lock.
The Python-asyncio threading model means simple dict/list ops are
already atomic within a single coroutine, but ``_evict_stale`` and
``_remove_job`` straddle several mutations, and a future move to
threading or to multiprocessing (with shared state) would expose the
fragility. ``threading.RLock`` is the cheap defensive choice — sync
callers see no API change, async callers don't have to ``await`` the
lock, and re-entrancy avoids self-deadlocks when (for example)
``create_job`` calls ``_evict_stale``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from app.schemas import JobManifest, JobStatus, Provider, SSEEvent

logger = logging.getLogger(__name__)

# Completed/failed jobs are evicted after this many seconds.
_DEFAULT_TTL_SECONDS = 3600  # 1 hour
_MAX_COMPLETED_JOBS = 200


class JobStore:
    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._jobs: dict[str, JobManifest] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._completed_at: dict[str, float] = {}  # job_id → monotonic timestamp
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_job(self, provider: Provider, model: str) -> str:
        with self._lock:
            self._evict_stale()
            job_id = str(uuid.uuid4())
            self._jobs[job_id] = JobManifest(
                job_id=job_id,
                provider=provider,
                model=model,
            )
            self._subscribers[job_id] = []
            return job_id

    def get_job(self, job_id: str) -> JobManifest | None:
        # Single dict.get is atomic in CPython; no lock needed.
        return self._jobs.get(job_id)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in kwargs.items():
                setattr(job, k, v)
            # Track when a job reaches terminal state for eviction
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                self._completed_at.setdefault(job_id, time.monotonic())

    def increment_counter(self, job_id: str, field: str, delta: int = 1) -> None:
        """Atomically read-increment-write a numeric counter on a job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            current = getattr(job, field, 0) or 0
            setattr(job, field, current + delta)

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def emit(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        """Push an SSEEvent to all subscriber queues."""
        sse = SSEEvent(event=event, data=data)
        # Snapshot the subscriber list under the lock so we don't iterate
        # a list that a concurrent subscribe/unsubscribe is mutating.
        with self._lock:
            queues = list(self._subscribers.get(job_id, []))
        for q in queues:
            try:
                q.put_nowait(sse)
            except asyncio.QueueFull:
                # Slow consumer — drop the event. Logged at debug so an
                # operator inspecting why a client missed updates can see
                # the back-pressure rather than diagnose it blindly.
                logger.debug(
                    "SSE subscriber queue full for job %s; dropping event %r",
                    job_id,
                    event,
                )

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(job_id, [])
            try:
                subs.remove(queue)
            except ValueError:
                pass

    async def stream_events(self, job_id: str) -> AsyncGenerator[SSEEvent, None]:
        """
        Yield SSEEvents for job_id.

        Subscribe first, THEN check status. The reverse order races: a
        terminal event emitted between the status check and `subscribe()`
        is dropped by `emit()` (no subscribers yet) and the consumer
        would hang on `queue.get()`. By subscribing first we own a queue
        before the terminal event can be missed; the post-subscribe
        status check then handles the "already terminal" case by
        draining whatever's in the queue and falling back to a synthetic
        terminal event when nothing arrived.

        Keepalive ping is sent every 30 s in the normal path; the
        generator exits on a 'completed' or 'failed' event.
        """
        queue = self.subscribe(job_id)
        try:
            # Re-check status AFTER subscribing. Three cases:
            #   - Terminal event landed BEFORE we subscribed → status is
            #     terminal, queue is empty → yield a synthetic terminal.
            #   - Terminal event landed BETWEEN subscribe and this check
            #     → status is terminal, the real terminal is in our
            #     queue → drain and yield it.
            #   - Job is still running → drop to the normal poll loop.
            job = self._jobs.get(job_id)
            if job is not None and job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                # Drain anything already buffered (events that arrived
                # between subscribe and this re-check) before falling
                # back to a synthetic terminal event. `get_nowait` is
                # safe here because we're single-threaded asyncio: no
                # producer can append while this sync loop runs.
                while not queue.empty():
                    buffered = queue.get_nowait()
                    yield buffered
                    if buffered.event in ("completed", "failed"):
                        return
                yield SSEEvent(event=job.status.value, data={"job_id": job_id})
                return

            while True:
                try:
                    event: SSEEvent = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event
                    if event.event in ("completed", "failed"):
                        break
                except TimeoutError:
                    yield SSEEvent(event="keepalive", data={})
        finally:
            self.unsubscribe(job_id, queue)

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict_stale(self) -> None:
        """Remove completed/failed jobs older than TTL or exceeding cap.

        Caller must hold ``self._lock`` — currently invoked only from
        ``create_job`` which acquires it. The RLock is re-entrant so the
        nested ``_remove_job`` calls below don't deadlock.
        """
        now = time.monotonic()
        expired = [jid for jid, ts in self._completed_at.items() if now - ts > self._ttl_seconds]
        for jid in expired:
            self._remove_job(jid)

        # Hard cap: if too many completed jobs, evict oldest first
        if len(self._completed_at) > _MAX_COMPLETED_JOBS:
            by_age = sorted(self._completed_at, key=self._completed_at.get)  # type: ignore[arg-type]
            excess = len(self._completed_at) - _MAX_COMPLETED_JOBS
            for jid in by_age[:excess]:
                self._remove_job(jid)

    def _remove_job(self, job_id: str) -> None:
        """Pop a job + its subscribers + its completion timestamp.

        Caller MUST hold ``self._lock`` — currently invoked only from
        ``_evict_stale``, which is itself called from ``create_job``
        under the lock. We don't re-acquire it here even though RLock
        would tolerate it: doing so would (a) violate the documented
        contract and (b) confuse future maintainers about the
        ownership model. The filesystem cleanup below intentionally
        runs OUTSIDE the lock since it touches disk and never re-enters
        the store.
        """
        self._jobs.pop(job_id, None)
        self._subscribers.pop(job_id, None)
        self._completed_at.pop(job_id, None)
        # Clean up disk storage for evicted jobs (best-effort, no lock).
        try:
            from app.storage import cleanup_job

            cleanup_job(job_id)
        except Exception:
            logger.debug("Failed to clean up disk for job %s", job_id, exc_info=True)
