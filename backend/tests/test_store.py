"""Tests for app.jobs.store (T-002 eviction, T-003 SSE queue overflow)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.jobs import store as store_module
from app.jobs.store import JobStore
from app.schemas import JobStatus, Provider

# ---------------------------------------------------------------------------
# Eviction by TTL
# ---------------------------------------------------------------------------


def test_completed_job_evicted_after_ttl():
    """Once a job is COMPLETED and TTL elapses, the next create_job evicts it."""
    store = JobStore(ttl_seconds=0)  # 0 = evict on any subsequent tick

    old_id = store.create_job(Provider.OPENAI, "test")
    store.update_job(old_id, status=JobStatus.COMPLETED)
    assert store.get_job(old_id) is not None

    # create_job() runs _evict_stale at the top. With TTL=0 and at least
    # one tick of monotonic clock advance, the completed job is purged.
    import time

    time.sleep(0.01)  # ensure now > completed_at
    new_id = store.create_job(Provider.OPENAI, "next")

    assert store.get_job(old_id) is None, "completed job should be evicted"
    assert store.get_job(new_id) is not None, "fresh job remains"


def test_running_job_not_evicted_by_ttl():
    """Only jobs in a terminal state are subject to TTL eviction."""
    store = JobStore(ttl_seconds=0)

    running_id = store.create_job(Provider.OPENAI, "test")
    # Don't mark terminal — leave as default (QUEUED)

    import time

    time.sleep(0.01)
    _ = store.create_job(Provider.OPENAI, "another")

    assert store.get_job(running_id) is not None


def test_failed_job_also_evicted():
    """JobStatus.FAILED triggers eviction same as COMPLETED."""
    store = JobStore(ttl_seconds=0)

    failed_id = store.create_job(Provider.OPENAI, "test")
    store.update_job(failed_id, status=JobStatus.FAILED, error="boom")

    import time

    time.sleep(0.01)
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

    store = JobStore(ttl_seconds=0)
    jid = store.create_job(Provider.OPENAI, "test")
    storage_mod.init_job_dirs(jid)
    job_path = storage_mod.job_dir(jid)
    assert job_path.exists()

    store.update_job(jid, status=JobStatus.COMPLETED)
    import time

    time.sleep(0.01)
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
