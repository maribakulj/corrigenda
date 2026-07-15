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
