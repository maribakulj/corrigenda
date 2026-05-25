"""Tests for app.jobs.store (T-002 eviction, T-003 SSE queue overflow)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.jobs import store as store_module
from app.jobs.store import JobStore
from app.schemas import JobStatus, Provider


def _patched_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Replace ``store_module.time.monotonic`` with a monotonically
    increasing fake clock that the test can advance by exact seconds.

    Removes the flaky ``time.sleep(0.01)`` pattern from the eviction
    tests (audit A12): no real wall-clock dependency, deterministic
    even on a slow CI runner.
    """
    clock = Clock()
    monkeypatch.setattr(store_module.time, "monotonic", clock.now)
    return clock


class Clock:
    def __init__(self) -> None:
        self._t = 1000.0

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ---------------------------------------------------------------------------
# Eviction by TTL
# ---------------------------------------------------------------------------


def test_completed_job_evicted_after_ttl(monkeypatch):
    """Once a job is COMPLETED and TTL elapses, the next create_job evicts it."""
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=0)  # 0 = evict on any subsequent tick

    old_id = store.create_job(Provider.OPENAI, "test")
    store.update_job(old_id, status=JobStatus.COMPLETED)
    assert store.get_job(old_id) is not None

    # create_job() runs _evict_stale at the top. With TTL=0 and any
    # forward tick on the clock, the completed job is purged.
    clock.advance(0.01)
    new_id = store.create_job(Provider.OPENAI, "next")

    assert store.get_job(old_id) is None, "completed job should be evicted"
    assert store.get_job(new_id) is not None, "fresh job remains"


def test_running_job_not_evicted_by_ttl(monkeypatch):
    """Only jobs in a terminal state are subject to TTL eviction."""
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=0)

    running_id = store.create_job(Provider.OPENAI, "test")
    # Don't mark terminal — leave as default (QUEUED)

    clock.advance(0.01)
    _ = store.create_job(Provider.OPENAI, "another")

    assert store.get_job(running_id) is not None


def test_failed_job_also_evicted(monkeypatch):
    """JobStatus.FAILED triggers eviction same as COMPLETED."""
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=0)

    failed_id = store.create_job(Provider.OPENAI, "test")
    store.update_job(failed_id, status=JobStatus.FAILED, error="boom")

    clock.advance(0.01)
    _ = store.create_job(Provider.OPENAI, "trigger")

    assert store.get_job(failed_id) is None


# ---------------------------------------------------------------------------
# Hard cap eviction
# ---------------------------------------------------------------------------


def test_completed_jobs_capped_oldest_first():
    """When more than _MAX_COMPLETED_JOBS terminal jobs exist, the
    oldest are evicted first regardless of TTL."""
    with patch.object(store_module, "_MAX_COMPLETED_JOBS", 3):
        store = JobStore(ttl_seconds=3600)  # TTL won't fire — test cap only

        completed_ids = []
        for _ in range(5):
            jid = store.create_job(Provider.OPENAI, "m")
            store.update_job(jid, status=JobStatus.COMPLETED)
            completed_ids.append(jid)

        # Trigger eviction by adding another job.
        _ = store.create_job(Provider.OPENAI, "trigger")

        # Oldest 2 should be evicted, newest 3 kept.
        kept = [jid for jid in completed_ids if store.get_job(jid) is not None]
        evicted = [jid for jid in completed_ids if store.get_job(jid) is None]
        assert len(kept) == 3
        assert len(evicted) == 2
        # FIFO: the first 2 created are the ones evicted.
        assert evicted == completed_ids[:2]


def test_eviction_cleans_disk(tmp_path, monkeypatch):
    """When a job is evicted, its on-disk directory is removed."""
    # Point storage at a tmp dir so we can assert filesystem cleanup.
    from app import storage as storage_mod

    monkeypatch.setattr(storage_mod, "_BASE_DIR", tmp_path)
    clock = _patched_clock(monkeypatch)

    store = JobStore(ttl_seconds=0)
    jid = store.create_job(Provider.OPENAI, "test")
    storage_mod.init_job_dirs(jid)
    job_path = storage_mod.job_dir(jid)
    assert job_path.exists()

    store.update_job(jid, status=JobStatus.COMPLETED)
    clock.advance(0.01)
    _ = store.create_job(Provider.OPENAI, "trigger")

    assert store.get_job(jid) is None
    assert not job_path.exists(), "evicted job's disk dir should be removed"


# ---------------------------------------------------------------------------
# SSE pub/sub mechanics
# ---------------------------------------------------------------------------


def test_emit_after_unsubscribe_does_not_reach_queue():
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")

    queue = store.subscribe(jid)
    store.unsubscribe(jid, queue)

    store.emit(jid, "test_event", {"hello": "world"})
    assert queue.empty()


def test_emit_reaches_all_subscribers():
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")

    q1 = store.subscribe(jid)
    q2 = store.subscribe(jid)
    store.emit(jid, "event_x", {"i": 1})

    assert q1.qsize() == 1
    assert q2.qsize() == 1


def test_sse_queue_drops_when_full_without_raising():
    """maxsize=500 — when full, emit() drops silently rather than raising
    or blocking the orchestrator. Slow consumers cost some events,
    not pipeline progress."""
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")
    queue = store.subscribe(jid)

    # Fill to capacity.
    for i in range(500):
        store.emit(jid, "fill", {"i": i})
    assert queue.qsize() == 500

    # Overflow event is dropped, no exception bubbles up.
    store.emit(jid, "overflow", {"i": 999})
    assert queue.qsize() == 500


# ---------------------------------------------------------------------------
# stream_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_fast_path_for_already_completed_job():
    """If the job is already in a terminal state when stream_events starts,
    yield a single synthetic terminal event and exit."""
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")
    store.update_job(jid, status=JobStatus.COMPLETED)

    events = []
    async for ev in store.stream_events(jid):
        events.append(ev)

    assert len(events) == 1
    assert events[0].event == "completed"
    assert events[0].data == {"job_id": jid}


@pytest.mark.asyncio
async def test_stream_events_exits_on_terminal_event():
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")

    # Run consumer concurrently with producer.
    async def producer():
        await asyncio.sleep(0.01)
        store.emit(jid, "info", {"msg": "hello"})
        await asyncio.sleep(0.01)
        store.emit(jid, "completed", {"job_id": jid})

    events = []

    async def consumer():
        async for ev in store.stream_events(jid):
            events.append(ev)

    await asyncio.gather(producer(), consumer())

    assert [e.event for e in events] == ["info", "completed"]


@pytest.mark.asyncio
async def test_stream_events_does_not_lose_terminal_during_subscribe_race(monkeypatch):
    """Roadmap L9 (R3) — there was a race between the initial fast-path
    status check and the `subscribe()` call:

      1. stream_events reads `job.status` → QUEUED, fast-path skipped.
      2. Another path (a worker thread, an awaited LLM callback, the
         pipeline coroutine) sets `status = COMPLETED` and calls
         `emit("completed", ...)`. Because we haven't subscribed yet,
         emit finds zero subscribers and silently drops the event.
      3. stream_events finally calls `subscribe()` → too late.
      4. `queue.get()` awaits forever; the SSE client never sees a
         terminal event.

    The fix subscribes FIRST, then re-checks status. With the new
    ordering, the post-subscribe check finds the terminal status and
    yields a synthetic terminal event, closing the stream cleanly.

    To force the race deterministically we monkey-patch `subscribe` so
    the racing `update_job + emit` happens INSIDE the subscribe call
    (after our caller has decided to subscribe, before the queue is
    attached). The OLD logic times out — the terminal event was emitted
    while we had no queue. The FIX yields a synthetic completed event
    and returns immediately.
    """
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")

    original_subscribe = store.subscribe

    def _racy_subscribe(job_id: str):
        # Simulate the race: status flips to COMPLETED and the terminal
        # event is emitted RIGHT BEFORE our queue gets attached. The
        # emit finds 0 subscribers and drops the event on the floor.
        store.update_job(job_id, status=JobStatus.COMPLETED)
        store.emit(job_id, "completed", {"job_id": job_id})
        return original_subscribe(job_id)

    monkeypatch.setattr(store, "subscribe", _racy_subscribe)

    events: list = []
    # 2-second budget is generous: the fix yields a synthetic event
    # synchronously after subscribe; the bug hangs on `queue.get()` for
    # the full keepalive period (30 s).
    try:
        async with asyncio.timeout(2.0):
            async for ev in store.stream_events(jid):
                events.append(ev)
                if ev.event in ("completed", "failed"):
                    break
    except TimeoutError:
        pass

    assert events, (
        "stream_events yielded nothing — the terminal event was lost in "
        "the race between the initial status check and subscribe(). The "
        "post-subscribe re-check must synthesise a terminal event when "
        "the status is already terminal."
    )
    assert events[-1].event == "completed", (
        f"expected terminal completed event, got {[e.event for e in events]}"
    )


# ---------------------------------------------------------------------------
# Subscriber cap (L10/F10) — prevent SSE-connection-flood DoS
# ---------------------------------------------------------------------------


def test_subscribe_rejects_beyond_per_job_cap():
    """L10/F10 — `JobStore.subscribe` was unbounded: an attacker could
    open thousands of SSE connections to one job_id, each owning a
    500-slot `asyncio.Queue`. With ~thousand subscribers × 500 events
    × event size, this is a cheap memory-DoS on the single-worker
    server (no auth required since job_id is the only "secret").

    The fix caps the per-job subscriber list at `JobStore.MAX_SUBSCRIBERS_PER_JOB`
    and raises `RuntimeError` when exceeded. The SSE route handler
    catches that and returns 503.
    """
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")

    queues = []
    cap = JobStore.MAX_SUBSCRIBERS_PER_JOB
    for _ in range(cap):
        queues.append(store.subscribe(jid))
    assert len(queues) == cap

    with pytest.raises(RuntimeError, match="subscriber cap"):
        store.subscribe(jid)


def test_subscriber_count_reports_current_size():
    """Companion of the cap test — the SSE route handler reads
    `subscriber_count(job_id)` to decide whether to 503 BEFORE
    calling subscribe (avoiding the after-headers exception path).
    """
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")
    assert store.subscriber_count(jid) == 0

    q1 = store.subscribe(jid)
    q2 = store.subscribe(jid)
    assert store.subscriber_count(jid) == 2

    store.unsubscribe(jid, q1)
    assert store.subscriber_count(jid) == 1

    store.unsubscribe(jid, q2)
    assert store.subscriber_count(jid) == 0


@pytest.mark.asyncio
async def test_stream_events_yields_synthetic_error_when_cap_exhausted():
    """L10/F10 — when `subscribe()` raises because the cap is exhausted,
    `stream_events` must yield a clean synthetic ``error`` event and
    return, NOT propagate the RuntimeError (which would surface as a
    generic 500 / silent disconnect to the SSE client AFTER headers
    had started flushing).
    """
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")

    # Fill the cap with live subscribers.
    _filler = [store.subscribe(jid) for _ in range(JobStore.MAX_SUBSCRIBERS_PER_JOB)]

    events = []
    async for ev in store.stream_events(jid):
        events.append(ev)
    # Generator must terminate (single synthetic event), not hang.
    assert len(events) == 1
    assert events[0].event == "error"
    assert events[0].data.get("reason") == "subscriber_cap_reached"


def test_subscribe_cap_recovers_after_unsubscribe():
    """Cap is on CURRENT subscribers, not cumulative — unsubscribing
    frees a slot so a new subscriber can take it. Otherwise a job
    that's had MAX subscribers over its lifetime would be permanently
    unsubscribable."""
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "test")
    cap = JobStore.MAX_SUBSCRIBERS_PER_JOB

    queues = [store.subscribe(jid) for _ in range(cap)]
    with pytest.raises(RuntimeError):
        store.subscribe(jid)

    store.unsubscribe(jid, queues[0])
    # Slot freed — must accept a fresh subscriber.
    store.subscribe(jid)


# ---------------------------------------------------------------------------
# Locking contract
# ---------------------------------------------------------------------------


def test_remove_job_is_invoked_under_lock_during_eviction(monkeypatch):
    """Roadmap remediation S1 — `_remove_job` mutates three dicts AND
    calls a filesystem cleanup, so the caller MUST hold `self._lock`.
    The L6 fix removed the in-method re-acquire on the grounds that
    `_evict_stale` (the only caller) is itself called from `create_job`
    under the lock. This test pins the contract so a future refactor
    that adds a new caller WITHOUT the lock trips here rather than
    causing a subtle race in production.

    We spy on `_remove_job` and record whether the RLock is owned by
    the current thread at each call. The check uses the CPython-stable
    `_is_owned()` private method — it's the documented way to test RLock
    ownership and the same idiom used by asyncio internals.
    """
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=0)

    # Set up an evictable job: completed, with a timestamp older than
    # `ttl_seconds=0` will tolerate after any forward tick.
    jid = store.create_job(Provider.OPENAI, "test")
    store.update_job(jid, status=JobStatus.COMPLETED)
    clock.advance(0.01)

    lock_states: list[bool] = []
    original_remove = store._remove_job

    def _spy(job_id: str) -> None:
        lock_states.append(store._lock._is_owned())  # type: ignore[attr-defined]
        original_remove(job_id)

    monkeypatch.setattr(store, "_remove_job", _spy)

    # Triggers _evict_stale, which calls _remove_job for the stale job.
    store.create_job(Provider.OPENAI, "next")

    assert lock_states, "_remove_job was never invoked — eviction did not fire"
    assert all(lock_states), (
        f"_remove_job called without holding self._lock at some point: "
        f"{lock_states}. A new caller has been added that does not enter "
        f"the lock first — restore the locking discipline."
    )
