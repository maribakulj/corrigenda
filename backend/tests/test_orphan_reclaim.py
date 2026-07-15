"""Plan V3.2 — orphan job directories are reclaimed at startup.

The JobStore is in-memory: after a restart, directories on a mounted
volume belong to job_ids the API no longer knows (every endpoint 404s)
and the TTL sweep can never reclaim them — it only iterates
``_completed_at``, which died with the process. Startup now deletes
them instead of letting a "persistent" volume fill with dead weight.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.jobs.store import JobStore
from app.schemas import Provider


def test_reclaim_deletes_unknown_dirs_and_keeps_known_ones(tmp_path):
    store = JobStore()
    known_id = store.create_job(Provider.OPENAI, "m")

    (tmp_path / known_id).mkdir()
    (tmp_path / "dead-job-1").mkdir()
    (tmp_path / "dead-job-2" / "output").mkdir(parents=True)
    (tmp_path / "stray-file.txt").write_text("not a dir")

    reclaimed = store.reclaim_orphans(tmp_path)

    assert reclaimed == 2
    assert (tmp_path / known_id).exists(), "a dir with a live record must survive"
    assert not (tmp_path / "dead-job-1").exists()
    assert not (tmp_path / "dead-job-2").exists()
    assert (tmp_path / "stray-file.txt").exists(), "only directories are touched"


def test_reclaim_respects_the_grace_window(tmp_path):
    store = JobStore()
    fresh = tmp_path / "very-fresh"
    fresh.mkdir()  # mtime = now

    assert store.reclaim_orphans(tmp_path, grace_seconds=3600) == 0
    assert fresh.exists()


def test_reclaim_handles_a_missing_base_dir(tmp_path):
    assert JobStore().reclaim_orphans(tmp_path / "nope") == 0


def test_startup_reclaims_pre_restart_directories(tmp_path, monkeypatch):
    """Integration: the lifespan handler sweeps orphans before serving."""
    from app import storage as storage_module
    from app.main import create_app

    base = tmp_path / "jobs"
    base.mkdir()
    # Simulate a pre-restart leftover on a "persistent" volume.
    leftover = base / "job-from-before-the-restart"
    (leftover / "output").mkdir(parents=True)
    (leftover / "output" / "a.corrected.xml").write_text("<alto/>")
    # Age it past any grace.
    old = time.time() - 7200
    import os

    os.utime(leftover, (old, old))

    monkeypatch.setattr(storage_module, "_BASE_DIR", base)
    app = create_app()
    with TestClient(app):
        assert not leftover.exists(), "startup must reclaim orphan job dirs"
