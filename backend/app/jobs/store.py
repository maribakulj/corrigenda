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

from app.schemas import (
    CorrectionReport,
    DocumentManifest,
    JobManifest,
    JobStatus,
    PipelineEventType,
    Provider,
    SSEEvent,
)

logger = logging.getLogger(__name__)

# Terminal jobs are evicted after this many seconds.
_DEFAULT_TTL_SECONDS = 3600  # 1 hour
_MAX_COMPLETED_JOBS = 200

#: Every state after which a job never changes again (eviction-eligible).
_TERMINAL_STATES = frozenset(
    {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_FALLBACKS, JobStatus.FAILED}
)


class JobStore:
    # L10/F10 — per-job SSE subscriber cap. Each subscriber owns a
    # 500-slot `asyncio.Queue` allocated by `subscribe()`. Without a
    # cap, an attacker can open thousands of SSE connections to one
    # job_id and pin ~500 events × N queues × event_size in memory —
    # a cheap memory-DoS on the single-worker server (no auth: the
    # job_id is the only "secret" and is often visible in operator
    # logs anyway). 10 concurrent SSE subscribers per job is well
    # above any legitimate UX (a job has 1 maker plus maybe a few
    # observers); legitimate consumers that lose their slot can
    # poll `/api/jobs/{id}` instead.
    MAX_SUBSCRIBERS_PER_JOB: int = 10

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
        # Opportunistic eviction (kept for burst hygiene); the periodic
        # sweep() is the guaranteed path. _evict_stale manages the lock
        # itself and does its disk I/O outside it (P1-4).
        self._evict_stale()
        with self._lock:
            job_id = str(uuid.uuid4())
            self._jobs[job_id] = JobManifest(
                job_id=job_id,
                provider=provider,
                model=model,
            )
            self._subscribers[job_id] = []
            return job_id

    def get_job(self, job_id: str) -> JobManifest | None:
        """Return a SHALLOW snapshot of the job (P1-3, honest contract).

        The top-level field dict is copied under the lock, so the eight
        scalar reads of a JobStatusResponse always see one consistent
        update (no torn payload). Sub-objects (``document_manifest``,
        ``report``) are SHARED with the live job, NOT deep-copied — a
        deep copy costs ~17 ms per 800 lines (measured) and would be
        paid on every status poll of a large corpus.

        Reader contract that makes the shallow copy safe:
        - while a job is non-terminal, only scalar fields may be read
          (the status endpoint does exactly that); the live manifest is
          being mutated by the pipeline during the run;
        - the heavy sub-objects are only consumed through
          ``get_completed_job`` (trace/diff/layout), which requires a
          TERMINAL state — after which nothing mutates them, and
          ``update_job`` REPLACES sub-objects instead of mutating them
          in place, so a reader holding the old one sees a coherent
          value. A future shared (multi-worker) store returns
          deserialized copies by construction.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return job.model_copy()

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        document_manifest: DocumentManifest | None = None,
        total_lines: int | None = None,
        lines_modified: int | None = None,
        chunks_total: int | None = None,
        retries: int | None = None,
        fallbacks: int | None = None,
        duration_seconds: float | None = None,
        error: str | None = None,
        images: dict[str, str] | None = None,
        report: CorrectionReport | None = None,
        token_hash: str | None = None,
    ) -> None:
        """Update mutable fields on the job manifest. None means "do not touch".

        Misnamed kwargs are caught at definition time by the static type
        checker; bad values for typed fields raise ``ValidationError``
        at assignment thanks to ``JobManifest.model_config`` having
        ``validate_assignment=True`` (audit F6).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if status is not None:
                job.status = status
            if document_manifest is not None:
                job.document_manifest = document_manifest
            if total_lines is not None:
                job.total_lines = total_lines
            if lines_modified is not None:
                job.lines_modified = lines_modified
            if chunks_total is not None:
                job.chunks_total = chunks_total
            if retries is not None:
                job.retries = retries
            if fallbacks is not None:
                job.fallbacks = fallbacks
            if duration_seconds is not None:
                job.duration_seconds = duration_seconds
            if error is not None:
                job.error = error
            if images is not None:
                job.images = images
            if report is not None:
                job.report = report
            if token_hash is not None:
                job.token_hash = token_hash
            # Track when a job reaches terminal state for eviction.
            # Uses the full terminal set — COMPLETED_WITH_FALLBACKS (P0-1)
            # included; forgetting a terminal state here means the job is
            # NEVER TTL-evicted.
            if job.status in _TERMINAL_STATES:
                self._completed_at.setdefault(job_id, time.monotonic())

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
            # L10/B7 — refuse to attach a queue to a job we don't know
            # about. Pre-fix this used `setdefault(job_id, [])` which
            # silently recreated an orphan subscriber entry for any
            # caller (e.g. SSE reconnect after eviction); the queue
            # then waited 30 s for a keepalive that nothing would
            # ever feed, leaking the entry until the next eviction.
            if job_id not in self._jobs:
                raise LookupError(f"unknown or evicted job: {job_id!r}")
            subs = self._subscribers.setdefault(job_id, [])
            if len(subs) >= self.MAX_SUBSCRIBERS_PER_JOB:
                # Subscriber cap reached. ``stream_events`` catches this and
                # yields a synthetic ``error`` SSE event to the client, so the
                # caller never needs a pre-flight count check.
                raise RuntimeError(
                    f"subscriber cap reached for job {job_id} (max {self.MAX_SUBSCRIBERS_PER_JOB})"
                )
            subs.append(q)
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

        Subscriber-cap handling (L10/F10): if the per-job cap is
        already exhausted, `subscribe()` raises RuntimeError; we
        translate that into a single synthetic ``error`` event so the
        client sees a clean refusal instead of a generic 500 / silent
        disconnect.

        Unknown-job handling (L10/B7): if the caller subscribes to a
        job_id that was evicted (or never existed), `subscribe()`
        raises LookupError; same pattern — yield one synthetic
        ``error`` event with reason ``job_not_found`` so the SSE
        client closes cleanly instead of hanging on a queue nothing
        will feed.
        """
        try:
            queue = self.subscribe(job_id)
        except LookupError as exc:
            yield SSEEvent(
                event="error",
                data={"reason": "job_not_found", "message": str(exc)},
            )
            return
        except RuntimeError as exc:
            yield SSEEvent(
                event="error",
                data={"reason": "subscriber_cap_reached", "message": str(exc)},
            )
            return
        try:
            # Re-check status AFTER subscribing. Three cases:
            #   - Terminal event landed BEFORE we subscribed → status is
            #     terminal, queue is empty → yield a synthetic terminal.
            #   - Terminal event landed BETWEEN subscribe and this check
            #     → status is terminal, the real terminal is in our
            #     queue → drain and yield it.
            #   - Job is still running → drop to the normal poll loop.
            job = self._jobs.get(job_id)
            # Audit P1 — must include COMPLETED_WITH_FALLBACKS: a degraded
            # success that finished before the client subscribed would
            # otherwise drop to the poll loop and hang forever (its real
            # terminal event already fired and will never come again).
            if job is not None and job.status in _TERMINAL_STATES:
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
                # Synthetic terminal. The event NAME must be one the
                # client listens for ("completed"/"failed"), NOT the raw
                # status value ("completed_with_fallbacks" is not a
                # listener) — and it must carry the full payload the live
                # 'completed' event does, so the client doesn't read
                # undefined fields. hyphen_pairs_total isn't stored on the
                # manifest; 0 is a safe default the client tolerates.
                if job.status == JobStatus.FAILED:
                    yield SSEEvent(
                        event="failed",
                        data={"job_id": job_id, "error": job.error or "job failed"},
                    )
                else:
                    yield SSEEvent(
                        event="completed",
                        data={
                            "job_id": job_id,
                            "total_lines": job.total_lines,
                            "lines_modified": job.lines_modified,
                            "hyphen_pairs_total": 0,
                            "chunks_total": job.chunks_total,
                            "duration_seconds": job.duration_seconds or 0.0,
                            "status": job.status.value,
                            "fallbacks": job.fallbacks,
                        },
                    )
                return

            while True:
                try:
                    event: SSEEvent = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event
                    if event.event in ("completed", "failed"):
                        break
                except TimeoutError:
                    yield SSEEvent(event=PipelineEventType.KEEPALIVE, data={})
        finally:
            self.unsubscribe(job_id, queue)

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _collect_stale_locked(self) -> list[str]:
        """Pop stale terminal jobs from the in-memory structures and return
        their ids for OUT-OF-LOCK disk cleanup.

        Caller must hold ``self._lock``. P1-4 — the historical version
        ran ``shutil.rmtree`` (hundreds of MB, seconds of I/O) while
        still holding the global lock, stalling every create/update/SSE
        emit — and the event loop itself — for the duration.
        """
        now = time.monotonic()
        stale = [jid for jid, ts in self._completed_at.items() if now - ts > self._ttl_seconds]
        # Hard cap: if too many completed jobs, evict oldest first
        if len(self._completed_at) > _MAX_COMPLETED_JOBS:
            by_age = sorted(self._completed_at, key=self._completed_at.get)  # type: ignore[arg-type]
            excess = len(self._completed_at) - _MAX_COMPLETED_JOBS
            for jid in by_age[:excess]:
                if jid not in stale:
                    stale.append(jid)
        for jid in stale:
            self._pop_job_locked(jid)
        return stale

    def _evict_stale(self) -> None:
        """Evict stale jobs. Takes the lock itself; disk cleanup happens
        AFTER the lock is released (P1-4)."""
        with self._lock:
            stale = self._collect_stale_locked()
        for jid in stale:
            self._cleanup_disk(jid)

    def sweep(self) -> int:
        """Public periodic-eviction entry point (P1-4).

        Historically eviction only ran inside ``create_job``: a server
        that stopped receiving new jobs kept every expired job's files
        on disk forever. The lifespan task calls this on an interval.
        Returns the number of jobs evicted (for the sweep log line).
        """
        with self._lock:
            stale = self._collect_stale_locked()
        for jid in stale:
            self._cleanup_disk(jid)
        return len(stale)

    def delete_job(self, job_id: str) -> None:
        """Remove a job's record, subscribers and disk artefacts.

        P1-10 — public rollback seam: ``create_job``'s HTTP handler
        registers the job before extraction/parsing/validation, so any
        failure in that window must delete the half-created job instead
        of leaving it QUEUED forever (a never-terminal job is never
        TTL-evicted). Also usable by an explicit user-facing delete.
        Disk cleanup runs after the lock is released (P1-4).
        """
        with self._lock:
            self._pop_job_locked(job_id)
        self._cleanup_disk(job_id)

    def _pop_job_locked(self, job_id: str) -> None:
        """Pop a job + its subscribers + its completion timestamp from the
        in-memory structures ONLY. Caller must hold ``self._lock``; disk
        cleanup is the caller's responsibility, outside the lock."""
        self._jobs.pop(job_id, None)
        self._subscribers.pop(job_id, None)
        self._completed_at.pop(job_id, None)

    @staticmethod
    def _cleanup_disk(job_id: str) -> None:
        """Best-effort disk cleanup — must NEVER be called under the lock."""
        try:
            from app.storage import cleanup_job

            cleanup_job(job_id)
        except Exception:
            logger.debug("Failed to clean up disk for job %s", job_id, exc_info=True)
