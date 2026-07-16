"""Plan V2.4 — capability tokens never travel in URLs.

Query strings leak into reverse-proxy/ingress/APM logs — the layer the
app is documented to sit behind and cannot redact. The full token used
to ride ``?token=`` for EventSource/<img>/downloads; it is now
header-only, and the header-less surfaces use short-lived signed
credentials scoped to ONE job and ONE purpose.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.signed_urls import sign_url_credential, verify_url_credential
from tests.test_api import MockProvider

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# Credential unit properties
# ---------------------------------------------------------------------------


def test_credential_roundtrip():
    cred = sign_url_credential("j1", "events", ttl_seconds=60)
    assert verify_url_credential("j1", "events", cred)


def test_credential_is_scoped_to_job_and_purpose():
    cred = sign_url_credential("j1", "events", ttl_seconds=60)
    assert not verify_url_credential("j2", "events", cred), "must not open another job"
    assert not verify_url_credential("j1", "images", cred), "must not open another purpose"


def test_credential_expires(monkeypatch):
    cred = sign_url_credential("j1", "events", ttl_seconds=60)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 120)
    assert not verify_url_credential("j1", "events", cred)


@pytest.mark.parametrize("bad", [None, "", "garbage", "123", ".mac", "notanint.mac"])
def test_credential_never_raises_on_malformed_input(bad):
    assert verify_url_credential("j1", "events", bad) is False


def test_tampered_expiry_is_rejected():
    cred = sign_url_credential("j1", "events", ttl_seconds=60)
    exp, _, mac = cred.partition(".")
    assert not verify_url_credential("j1", "events", f"{int(exp) + 9999}.{mac}")


# ---------------------------------------------------------------------------
# API integration
# ---------------------------------------------------------------------------


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


def _create(client: TestClient) -> dict:
    r = client.post(
        "/api/jobs",
        data={"provider": "openai", "api_key": "fake-key", "model": "mock-model"},
        files=[("files", ("sample.xml", SAMPLE_XML.read_bytes(), "application/xml"))],
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_create_returns_signed_events_url_without_the_token(client):
    body = _create(client)
    assert body["events_url"].startswith(f"/api/jobs/{body['job_id']}/events?sig=")
    assert body["job_token"] not in body["events_url"]


def test_query_token_is_no_longer_accepted_anywhere(client):
    body = _create(client)
    job_id, token = body["job_id"], body["job_token"]
    # The old ?token= transport must be dead on every endpoint.
    assert client.get(f"/api/jobs/{job_id}?token={token}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/events?token={token}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/download?token={token}").status_code == 404
    # Header access still works.
    assert client.get(f"/api/jobs/{job_id}", headers={"X-Job-Token": token}).status_code == 200


def test_events_sig_opens_only_the_events_route(client):
    body = _create(client)
    job_id = body["job_id"]
    sig = body["events_url"].split("sig=")[1]
    # The events credential must NOT open any other endpoint.
    assert client.get(f"/api/jobs/{job_id}?sig={sig}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/download?sig={sig}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/images/x.png?sig={sig}").status_code == 404


def test_events_route_rejects_a_bad_sig(client):
    body = _create(client)
    job_id = body["job_id"]
    r = client.get(f"/api/jobs/{job_id}/events?sig=123.deadbeef")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Events-URL renewal (the SSE credential no longer depends on the job budget)
# ---------------------------------------------------------------------------


def _wait_terminal(client: TestClient, job_id: str, token: str) -> str:
    """Poll the status endpoint until the background job settles."""
    deadline = time.time() + 15
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}", headers={"X-Job-Token": token})
        assert r.status_code == 200
        status = r.json()["status"]
        if status in ("completed", "completed_with_fallbacks", "failed", "cancelled"):
            return status
        time.sleep(0.1)
    raise AssertionError("job never reached a terminal state")


def test_events_url_renewal_is_token_gated(client):
    body = _create(client)
    job_id, token = body["job_id"], body["job_token"]

    # No token → 404 (indistinguishable from a missing job); sig-based
    # access must not mint fresh credentials either (renewal requires
    # the REAL capability token).
    assert client.get(f"/api/jobs/{job_id}/events-url").status_code == 404

    r = client.get(f"/api/jobs/{job_id}/events-url", headers={"X-Job-Token": token})
    assert r.status_code == 200
    url = r.json()["events_url"]
    assert url.startswith(f"/api/jobs/{job_id}/events?sig=")
    sig = url.split("sig=")[1]
    assert verify_url_credential(job_id, "events", sig)


def test_creation_events_sig_is_short_lived_not_run_scoped(client, monkeypatch):
    """With renewal available, the creation-time credential no longer needs
    to outlive the whole run — and must not inherit the job timeout: with
    JOB_TIMEOUT_SECONDS=0 (timeout disabled) the historical formula
    (timeout + 600) minted a 10-minute credential for an unbounded run,
    after which the live stream could never be re-established."""
    from app.jobs import runner as runner_module

    monkeypatch.setattr(runner_module, "DEFAULT_JOB_TIMEOUT_SECONDS", 0)
    body = _create(client)
    sig = body["events_url"].split("sig=")[1]
    exp = int(sig.split(".")[0])
    ttl = exp - time.time()
    assert 0 < ttl <= 3600, f"creation sig must be short-lived, got {ttl:.0f}s"


# ---------------------------------------------------------------------------
# Signed download (browser-native streaming, token never in the URL)
# ---------------------------------------------------------------------------


def test_download_url_mints_a_working_streaming_credential(client):
    body = _create(client)
    job_id, token = body["job_id"], body["job_token"]
    assert _wait_terminal(client, job_id, token).startswith("completed")

    # Renewal is token-gated like every job endpoint.
    assert client.get(f"/api/jobs/{job_id}/download-url").status_code == 404
    r = client.get(f"/api/jobs/{job_id}/download-url", headers={"X-Job-Token": token})
    assert r.status_code == 200
    url = r.json()["download_url"]
    assert url.startswith(f"/api/jobs/{job_id}/download?sig=")
    assert token not in url

    # The signed URL streams the artefact with NO header — that is the
    # whole point (browser-native download instead of a blob in memory).
    dl = client.get(url)
    assert dl.status_code == 200
    assert "attachment" in dl.headers.get("content-disposition", "")


def test_download_sig_is_purpose_scoped(client):
    body = _create(client)
    job_id, token = body["job_id"], body["job_token"]
    assert _wait_terminal(client, job_id, token).startswith("completed")
    r = client.get(f"/api/jobs/{job_id}/download-url", headers={"X-Job-Token": token})
    sig = r.json()["download_url"].split("sig=")[1]
    # A download credential opens nothing else.
    assert client.get(f"/api/jobs/{job_id}?sig={sig}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/events?sig={sig}").status_code == 404


def test_download_url_refuses_non_terminal_jobs(client):
    store = client.app.state.job_store
    from app.schemas import Provider

    job_id = store.create_job(Provider.OPENAI, "mock")  # stays QUEUED, ungated
    r = client.get(f"/api/jobs/{job_id}/download-url")
    assert r.status_code == 400
