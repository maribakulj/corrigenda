"""Tests for BackgroundTaskRegistry (Stage 4.A)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.jobs.task_registry import BackgroundTaskRegistry


@pytest.mark.asyncio
async def test_spawn_runs_coroutine_and_clears_on_completion():
    reg = BackgroundTaskRegistry()
    sentinel: list[str] = []

    async def work():
        await asyncio.sleep(0)
        sentinel.append("done")

    task = reg.spawn(work())
    assert reg.active_count == 1
    await task
    # Yield once so done_callback can clear the registry.
    await asyncio.sleep(0)
    assert reg.active_count == 0
    assert sentinel == ["done"]


@pytest.mark.asyncio
async def test_spawn_keeps_strong_reference_against_gc():
    """Without a strong ref, asyncio.create_task can be GC'd mid-run."""
    reg = BackgroundTaskRegistry()
    completed = asyncio.Event()

    async def work():
        await asyncio.sleep(0.01)
        completed.set()

    reg.spawn(work())  # no caller-side reference
    await asyncio.wait_for(completed.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_spawn_after_shutdown_raises():
    reg = BackgroundTaskRegistry()
    await reg.shutdown()
    with pytest.raises(RuntimeError, match="shutting down"):
        reg.spawn(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_task_exception_is_logged_not_swallowed(caplog):
    reg = BackgroundTaskRegistry()

    async def boom():
        raise RuntimeError("simulated failure")

    with caplog.at_level(logging.ERROR, logger="app.jobs.task_registry"):
        reg.spawn(boom(), name="test-boom")
        # Yield until the task settles and the done_callback fires.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert any("simulated failure" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_tasks():
    reg = BackgroundTaskRegistry()
    finished: list[str] = []

    async def slow():
        await asyncio.sleep(0.05)
        finished.append("ok")

    reg.spawn(slow())
    await reg.shutdown(timeout=1.0)
    assert finished == ["ok"]


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks_past_deadline(caplog):
    reg = BackgroundTaskRegistry()

    async def hangs():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    reg.spawn(hangs(), name="hangs")
    with caplog.at_level(logging.WARNING, logger="app.jobs.task_registry"):
        await reg.shutdown(timeout=0.05)
    assert any("cancelled" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_shutdown_is_idempotent():
    reg = BackgroundTaskRegistry()
    await reg.shutdown()
    # Second call should be a no-op, not raise.
    await reg.shutdown()
