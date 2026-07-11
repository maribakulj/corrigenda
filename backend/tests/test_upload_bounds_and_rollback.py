"""P0-3 / P1-9 / P1-10 — bounded uploads, refused collisions, transactional
job creation.

Historically: only a PER-FILE size cap existed (30 × 100 MiB pinned
~3 GiB in the single-worker process), every ZIP got its OWN 500 MB
decompression budget, flattened name collisions silently overwrote an
earlier document (volume-1/page.xml lost to volume-2/page.xml), and any
failure between job registration and task spawn left a QUEUED job with
files on disk forever (never terminal → never TTL-evicted).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import MockProvider  # reuse the provider mock

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with mocked providers and an isolated storage dir."""
    from app import providers as prov_module
    from app import storage as storage_module
    from app.main import create_app

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")

    mock = MockProvider()
    orig = prov_module._REGISTRY.copy()
    for k in list(prov_module._REGISTRY.keys()):
        prov_module._REGISTRY[k] = mock
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    prov_module._REGISTRY.update(orig)


def _form(provider: str = "openai") -> dict:
    return {"provider": provider, "api_key": "fake-key", "model": "mock-model"}


def _xml_upload(filename: str, content: bytes | None = None):
    return ("files", (filename, content or SAMPLE_XML.read_bytes(), "application/xml"))


def _zip_with(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _job_store(client: TestClient):
    return client.app.state.job_store


# ---------------------------------------------------------------------------
# P0-3 — request-level caps
# ---------------------------------------------------------------------------


def test_too_many_files_rejected_before_reading(client, monkeypatch):
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILES", 3)
    files = [_xml_upload(f"f{i}.xml") for i in range(4)]
    r = client.post("/api/jobs", data=_form(), files=files)
    assert r.status_code == 413
    assert "Too many files" in r.json()["detail"]


def test_cumulative_bytes_rejected(client, monkeypatch):
    from app.api import jobs as jobs_api

    # Each file passes the per-file cap; the SUM must still be bounded.
    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILE_BYTES", 10_000)
    monkeypatch.setattr(jobs_api, "_MAX_TOTAL_UPLOAD_BYTES", 15_000)
    payload = b"x" * 9_000
    files = [
        _xml_upload("a.xml", payload),
        _xml_upload("b.xml", payload),  # total 18 000 > 15 000
    ]
    r = client.post("/api/jobs", data=_form(), files=files)
    assert r.status_code == 413
    assert "total request limit" in r.json()["detail"]


def test_zip_decompression_budget_is_shared_across_archives(client, monkeypatch):
    """Historically each ZIP got its own budget: N archives could stage
    N × 500 MB. The budget is per job now."""
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "_MAX_ZIP_EXTRACTED_BYTES", 20_000)
    xml = SAMPLE_XML.read_bytes()
    assert len(xml) < 20_000
    # Each archive alone fits the budget; together they exceed it.
    z1 = _zip_with({"a.xml": xml})
    z2 = _zip_with({"b.xml": b"y" * (20_000 - len(xml) + 1)})
    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[
            ("files", ("one.zip", z1, "application/zip")),
            ("files", ("two.zip", z2, "application/zip")),
        ],
    )
    assert r.status_code == 400
    assert "budget" in r.json()["detail"]


# ---------------------------------------------------------------------------
# P1-9 — silent overwrites become explicit 400s
# ---------------------------------------------------------------------------


def test_flattened_zip_name_collision_is_refused(client):
    z = _zip_with(
        {
            "volume-1/page.xml": SAMPLE_XML.read_bytes(),
            "volume-2/page.xml": SAMPLE_XML.read_bytes(),
        }
    )
    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[("files", ("batch.zip", z, "application/zip"))],
    )
    assert r.status_code == 400
    assert "page.xml" in r.json()["detail"]


def test_direct_upload_name_collision_is_refused(client):
    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[_xml_upload("same.xml"), _xml_upload("same.xml")],
    )
    assert r.status_code == 400


def test_output_stem_collision_is_refused(client):
    """page.xml + page.alto would both write page_corrected.xml."""
    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[
            _xml_upload("page.xml"),
            ("files", ("page.alto", SAMPLE_XML.read_bytes(), "application/xml")),
        ],
    )
    assert r.status_code == 400
    assert "stem" in r.json()["detail"]


# ---------------------------------------------------------------------------
# P1-10 — transactional creation: no orphan QUEUED jobs
# ---------------------------------------------------------------------------


def _no_jobs_remain(client) -> bool:
    store = _job_store(client)
    return len(store._jobs) == 0  # test-only introspection


def test_parse_failure_rolls_back_job_and_disk(client, tmp_path):
    from app import storage as storage_module

    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[_xml_upload("broken.xml", b"this is not xml at all")],
    )
    assert r.status_code == 400
    assert _no_jobs_remain(client)
    base = storage_module._BASE_DIR
    assert not base.exists() or list(base.iterdir()) == []


def test_collision_failure_rolls_back_job_and_disk(client):
    from app import storage as storage_module

    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[_xml_upload("same.xml"), _xml_upload("same.xml")],
    )
    assert r.status_code == 400
    assert _no_jobs_remain(client)
    base = storage_module._BASE_DIR
    assert not base.exists() or list(base.iterdir()) == []


def test_successful_creation_still_works(client):
    r = client.post("/api/jobs", data=_form(), files=[_xml_upload("ok.xml")])
    assert r.status_code == 200
    assert "job_id" in r.json()


# ---------------------------------------------------------------------------
# P1-5 — admission control
# ---------------------------------------------------------------------------


def test_admission_refuses_when_at_capacity(client, monkeypatch):
    """The rate limit throttles requests, not concurrency — the task
    registry's live count now gates admissions with an explicit 503."""
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_ACTIVE_JOBS", 0)
    r = client.post("/api/jobs", data=_form(), files=[_xml_upload("ok.xml")])
    assert r.status_code == 503
    assert "capacity" in r.json()["detail"]
    assert r.headers.get("retry-after") == "30"
    assert _no_jobs_remain(client)  # refused BEFORE creating anything


def test_admission_allows_under_capacity(client, monkeypatch):
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_ACTIVE_JOBS", 4)
    r = client.post("/api/jobs", data=_form(), files=[_xml_upload("ok.xml")])
    assert r.status_code == 200
