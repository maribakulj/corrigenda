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
# L10/F6 — JobManifest must reject bad-typed setattr
# ---------------------------------------------------------------------------


def test_jobmanifest_rejects_invalid_status_via_setattr():
    """L10/F6 — `JobStore.update_job` loops `setattr(job, k, v)` over
    its kwargs. Without `validate_assignment=True` on the model,
    Pydantic v2 silently accepts wrong types — e.g.
    `update_job(jid, status="garbage")` writes the literal string
    into the status enum field. Downstream `job.status.value` (e.g.
    in the SSE generator and API response) then crashes with
    AttributeError far from the offending caller.

    With `validate_assignment=True`, the bad setattr raises
    ValidationError immediately, surfacing the real bug at the
    offending callsite.
    """
    from pydantic import ValidationError

    from app.schemas import JobManifest

    job = JobManifest(job_id="j1", provider=Provider.OPENAI, model="m")
    with pytest.raises(ValidationError):
        job.status = "totally not a JobStatus enum"  # type: ignore[assignment]


def test_update_job_rejects_wrong_typed_field():
    """L10/F6 integration — `update_job` should now propagate the
    ValidationError from the underlying setattr, not silently corrupt
    the field. We use a fresh JobStore to avoid touching other jobs."""
    from pydantic import ValidationError

    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "m")
    with pytest.raises(ValidationError):
        store.update_job(jid, status="garbage")


# ---------------------------------------------------------------------------
# L10/F7 — get_job must return a snapshot, not the live reference
# ---------------------------------------------------------------------------


def test_get_job_returns_snapshot_not_live_reference():
    """L10/F7 — pre-fix `get_job` returned the live `JobManifest` from
    `_jobs`. Callers reading multiple attributes could observe an
    inconsistent state when `update_job` mutated the same object
    mid-read. The fix returns `job.model_copy()` under the lock so
    the caller has a frozen-in-time snapshot.

    The contract pinned here: mutating the returned object MUST NOT
    affect the in-store record. Pre-fix, mutating the returned ref
    DID affect the store (because it was the store's own object).
    """
    store = JobStore()
    jid = store.create_job(Provider.OPENAI, "m")
    store.update_job(jid, total_lines=10, lines_modified=3)

    snapshot = store.get_job(jid)
    assert snapshot is not None
    # Mutate the snapshot.
    snapshot.lines_modified = 99
    # Re-fetch the store record; it must NOT reflect the mutation.
    fresh = store.get_job(jid)
    assert fresh is not None
    assert fresh.lines_modified == 3, (
        f"get_job returned a live reference instead of a snapshot — "
        f"mutating the result polluted the store (lines_modified={fresh.lines_modified})."
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


def test_subscribe_rejects_unknown_job_id_instead_of_leaking_entry():
    """L10/B7 — pre-fix `subscribe()` used
    `_subscribers.setdefault(job_id, []).append(q)`. After
    `_remove_job` evicted the job, a late `subscribe(job_id)` call
    (e.g. SSE reconnect after eviction) silently RE-created the entry
    in `_subscribers` and attached a queue that nothing would ever
    feed. The stream_events generator's post-subscribe status check
    then saw `_jobs.get(job_id) == None`, fell through to the normal
    poll loop, and hung forever on `queue.get()` while the orphan
    entry stayed in `_subscribers` until the next eviction sweep.

    After the fix, subscribing to an unknown job raises `LookupError`
    so no orphan entry is created.
    """
    store = JobStore()
    # No job created — `_jobs` is empty, `_subscribers` is empty.
    assert "ghost" not in store._subscribers

    with pytest.raises(LookupError, match="ghost"):
        store.subscribe("ghost")

    # Critical: no orphan list was left behind.
    assert "ghost" not in store._subscribers


def test_subscribe_rejects_evicted_job():
    """Symmetric to the previous test — same behaviour after the job
    has been evicted, not just for never-existed ids."""
    store = JobStore(ttl_seconds=0)
    jid = store.create_job(Provider.OPENAI, "test")
    store.update_job(jid, status=JobStatus.COMPLETED)
    # Trigger eviction via a subsequent create_job.
    import time

    time.sleep(0.01)
    store.create_job(Provider.OPENAI, "next")

    assert jid not in store._jobs, "test setup failed — job should be evicted"

    with pytest.raises(LookupError):
        store.subscribe(jid)
    assert jid not in store._subscribers


@pytest.mark.asyncio
async def test_stream_events_yields_synthetic_event_when_job_unknown():
    """L10/B7 — when `subscribe` raises LookupError, stream_events
    must yield a single synthetic ``error`` event with a meaningful
    reason and return cleanly (vs hanging on a queue that nothing
    will feed)."""
    store = JobStore()
    events = []
    async for ev in store.stream_events("never-existed"):
        events.append(ev)
    assert len(events) == 1
    assert events[0].event == "error"
    assert events[0].data.get("reason") == "job_not_found"


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


def test_eviction_pops_under_lock_and_cleans_disk_outside_it(monkeypatch):
    """P1-4 — the eviction contract, INVERTED from the historical one:
    the in-memory pop (`_pop_job_locked`) must run UNDER the lock, and
    the filesystem cleanup (`_cleanup_disk`, potentially seconds of
    rmtree I/O) must run OUTSIDE it. The old design ran rmtree while
    holding the global lock, stalling every create/update/SSE emit — and
    the event loop itself — for the duration of the delete.

    Uses the CPython-stable RLock `_is_owned()` — the documented idiom
    for testing lock ownership (same as asyncio internals).
    """
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=0)

    jid = store.create_job(Provider.OPENAI, "test")
    store.update_job(jid, status=JobStatus.COMPLETED)
    clock.advance(0.01)

    pop_lock_states: list[bool] = []
    cleanup_lock_states: list[bool] = []
    original_pop = store._pop_job_locked
    original_cleanup = store._cleanup_disk

    def _spy_pop(job_id: str) -> None:
        pop_lock_states.append(store._lock._is_owned())  # type: ignore[attr-defined]
        original_pop(job_id)

    def _spy_cleanup(job_id: str) -> None:
        cleanup_lock_states.append(store._lock._is_owned())  # type: ignore[attr-defined]
        original_cleanup(job_id)

    monkeypatch.setattr(store, "_pop_job_locked", _spy_pop)
    monkeypatch.setattr(store, "_cleanup_disk", _spy_cleanup)

    # Triggers the opportunistic eviction path.
    store.create_job(Provider.OPENAI, "next")

    assert pop_lock_states, "eviction never popped the stale job"
    assert all(pop_lock_states), "in-memory pop ran without the lock — torn reads possible"
    assert cleanup_lock_states, "disk cleanup never ran for the evicted job"
    assert not any(cleanup_lock_states), (
        "disk cleanup ran UNDER the lock — rmtree I/O stalls every "
        "store operation and the event loop (the exact P1-4 defect)"
    )


def test_sweep_evicts_without_new_job_creations(monkeypatch):
    """P1-4 — eviction used to fire only inside create_job: a server that
    stopped receiving jobs kept expired files forever. sweep() is the
    creation-independent path the periodic lifespan task calls."""
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=10)

    jid = store.create_job(Provider.OPENAI, "test")
    store.update_job(jid, status=JobStatus.COMPLETED)
    clock.advance(11)

    evicted = store.sweep()
    assert evicted == 1
    assert store.get_job(jid) is None


def test_completed_with_fallbacks_is_ttl_tracked(monkeypatch):
    """B1 regression net — the degraded-success terminal state must enter
    _completed_at like the others; forgetting a terminal state means the
    job is NEVER evicted."""
    clock = _patched_clock(monkeypatch)
    store = JobStore(ttl_seconds=10)

    jid = store.create_job(Provider.OPENAI, "test")
    store.update_job(jid, status=JobStatus.COMPLETED_WITH_FALLBACKS)
    clock.advance(11)

    assert store.sweep() == 1
    assert store.get_job(jid) is None
