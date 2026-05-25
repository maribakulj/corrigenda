"""In-memory job store with SSE fan-out and TTL eviction."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from app.schemas import (
    DocumentManifest,
    JobManifest,
    JobStatus,
    LineTrace,
    Provider,
    SSEEvent,
)

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

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_job(self, provider: Provider, model: str) -> str:
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
        return self._jobs.get(job_id)

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
        line_traces: dict[str, LineTrace] | None = None,
    ) -> None:
        """Update mutable fields on the job manifest. None means "do not touch".

        Misnamed kwargs are caught at definition time by the static type
        checker; bad values for typed fields raise ValidationError at
        assignment thanks to JobManifest.model_config["validate_assignment"].
        """
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
        if line_traces is not None:
            job.line_traces = line_traces
        # Track when a job reaches terminal state for eviction
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            self._completed_at.setdefault(job_id, time.monotonic())

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def emit(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        """Push an SSEEvent to all subscriber queues."""
        sse = SSEEvent(event=event, data=data)
        for q in self._subscribers.get(job_id, []):
            try:
                q.put_nowait(sse)
            except asyncio.QueueFull:
                pass  # slow consumer — drop

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id, [])
        try:
            subs.remove(queue)
        except ValueError:
            pass

    async def stream_events(self, job_id: str) -> AsyncGenerator[SSEEvent, None]:
        """
        Yield SSEEvents for job_id.

        If the job is already in a terminal state (completed/failed) when
        this generator starts, yield a synthetic terminal event immediately.
        Otherwise sends a keepalive ping every 30 s and exits on
        'completed' or 'failed' event.
        """
        # Fast-path: job already in terminal state
        job = self._jobs.get(job_id)
        if job is not None and job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            yield SSEEvent(event=job.status.value, data={"job_id": job_id})
            return

        queue = self.subscribe(job_id)
        try:
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
        """Remove completed/failed jobs older than TTL or exceeding cap."""
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
        self._jobs.pop(job_id, None)
        self._subscribers.pop(job_id, None)
        self._completed_at.pop(job_id, None)
        # Clean up disk storage for evicted jobs
        try:
            from app.storage import cleanup_job

            cleanup_job(job_id)
        except Exception:
            logger.debug("Failed to clean up disk for job %s", job_id, exc_info=True)
