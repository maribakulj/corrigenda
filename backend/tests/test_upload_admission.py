"""Plan V2.1 — upload-phase capacity reservation + disk streaming.

The running-jobs cap only bounds SPAWNED pipelines: N concurrent
requests could all buffer up to 200 MiB each before the authoritative
check let 4 spawn — an unbounded memory spike during the upload phase.
Uploads now (a) reserve a slot before any body byte is read, and
(b) stream to disk in 1 MiB chunks instead of accumulating bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import MockProvider

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import providers as prov_module
    from app import storage as storage_module
    from app.main import create_app

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")

    mock = MockProvider()
    orig = prov_module._REGISTRY.copy()
    for k in list(prov_module._REGISTRY.keys()):
        prov_module._REGISTRY[k] = mock
    app = create_app()
    app.state.limiter.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    prov_module._REGISTRY.update(orig)
    app.state.limiter.reset()


def _form() -> dict:
    return {"provider": "openai", "api_key": "fake-key", "model": "mock-model"}


def _upload(name: str = "sample.xml"):
    return [("files", (name, SAMPLE_XML.read_bytes(), "application/xml"))]


# ---------------------------------------------------------------------------
# Slot reservation
# ---------------------------------------------------------------------------


def test_upload_slots_exhausted_returns_503(client):
    # Simulate N in-flight uploads holding every slot.
    from app.api import jobs as jobs_api

    client.app.state.uploads_in_progress = jobs_api._MAX_CONCURRENT_UPLOADS
    try:
        r = client.post("/api/jobs", data=_form(), files=_upload())
        assert r.status_code == 503
        assert "upload capacity" in r.json()["detail"]
        assert "Retry-After" in r.headers
    finally:
        client.app.state.uploads_in_progress = 0


def test_upload_slot_is_released_after_success(client):
    r = client.post("/api/jobs", data=_form(), files=_upload())
    assert r.status_code == 200
    assert client.app.state.uploads_in_progress == 0


def test_upload_slot_is_released_after_rejection(client, monkeypatch):
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILE_BYTES", 10)
    r = client.post("/api/jobs", data=_form(), files=_upload())
    assert r.status_code == 413
    assert client.app.state.uploads_in_progress == 0


def test_ready_exposes_load_gauges(client):
    body = client.get("/health/ready").json()
    assert body["load"] == {"uploads_in_progress": 0, "jobs_running": 0}


# ---------------------------------------------------------------------------
# Disk streaming
# ---------------------------------------------------------------------------


def test_upload_staging_is_reclaimed_after_success(client, tmp_path):
    r = client.post("/api/jobs", data=_form(), files=_upload())
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    staging = tmp_path / "jobs" / job_id / "upload-staging"
    assert not staging.exists(), "upload staging must be reclaimed after extraction"
    # The XML itself was MOVED into the input dir, not copied.
    assert (tmp_path / "jobs" / job_id / "input" / "sample.xml").exists()


def test_streaming_caps_still_reject_incrementally(client, monkeypatch):
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILE_BYTES", 10_000)
    monkeypatch.setattr(jobs_api, "_MAX_TOTAL_UPLOAD_BYTES", 15_000)
    payload = b"x" * 9_000
    files = [
        ("files", ("a.xml", payload, "application/xml")),
        ("files", ("b.xml", payload, "application/xml")),  # 18k > 15k
    ]
    r = client.post("/api/jobs", data=_form(), files=files)
    assert r.status_code == 413
    assert "total request limit" in r.json()["detail"]


def test_rejected_upload_leaves_no_job_directory(client, tmp_path, monkeypatch):
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILE_BYTES", 10)
    r = client.post("/api/jobs", data=_form(), files=_upload())
    assert r.status_code == 413
    # The transactional rollback reclaims the job dir INCLUDING staging.
    leftovers = list((tmp_path / "jobs").glob("*"))
    assert leftovers == [], f"rejected upload left directories behind: {leftovers}"


# ---------------------------------------------------------------------------
# save_uploaded_files accepts staged Paths (unit)
# ---------------------------------------------------------------------------


def test_save_uploaded_files_moves_staged_paths(tmp_path, monkeypatch):
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    staged = tmp_path / "staging" / "0000_doc.xml"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(SAMPLE_XML.read_bytes())

    saved, images = storage_module.save_uploaded_files("j1", [("doc.xml", staged)])

    assert "doc.xml" in saved
    assert saved["doc.xml"].exists()
    assert not staged.exists(), "staged file must be MOVED (renamed), not copied"
    assert images == {}


def test_save_uploaded_files_opens_zip_from_path(tmp_path, monkeypatch):
    import io
    import zipfile

    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inner.xml", SAMPLE_XML.read_bytes())
    staged = tmp_path / "staging" / "0000_batch.zip"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(buf.getvalue())

    saved, _ = storage_module.save_uploaded_files("j2", [("batch.zip", staged)])

    assert "inner.xml" in saved
    assert saved["inner.xml"].read_bytes() == SAMPLE_XML.read_bytes()
