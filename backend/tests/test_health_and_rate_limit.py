"""Tests for /health/live, /health/ready, and slowapi rate limiting
(Stage 4.D + roadmap L2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client():
    app = create_app()
    # Reset the limiter between tests so the per-IP counter from a
    # previous test doesn't leak in. slowapi exposes the storage on
    # the limiter directly.
    app.state.limiter.reset()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


def test_health_live_always_returns_200(client: TestClient):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_ready_returns_status_and_checks(client: TestClient):
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["job_store"] == "ok"
    assert body["checks"]["storage_dir"] == "ok"


def test_legacy_health_still_works(client: TestClient):
    """Stage 4.D shouldn't break the /health route HF Spaces hits."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_ready_does_not_create_storage_dir(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L2 (B2) — readiness probe must NOT mutate the filesystem.

    A probe scraped every second by an orchestrator must not create
    directories as a side effect: doing so (a) masks a genuine
    'storage dir missing' bug since the probe silently provisions it,
    and (b) makes the endpoint non-idempotent in shared environments.
    """
    target = tmp_path / "should-not-be-created-by-probe"
    assert not target.exists()
    monkeypatch.setenv("JOB_STORAGE_DIR", str(target))

    client.get("/health/ready")

    # Whatever the response (200 or 503), the directory must NOT have
    # been created. The check is the side-effect, not the status code.
    assert not target.exists(), (
        "/health/ready created the storage dir on disk — readiness probes must be observation-only"
    )


def test_health_ready_returns_503_when_storage_not_writable(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L2 — exercise the degraded branch end-to-end.

    Uses a regular file as ``JOB_STORAGE_DIR`` so the writability
    check fails deterministically regardless of the user running the
    test (chmod-based read-only tricks break under root in CI
    containers).
    """
    blocker = tmp_path / "i-am-a-file-not-a-dir.txt"
    blocker.write_bytes(b"x")
    monkeypatch.setenv("JOB_STORAGE_DIR", str(blocker))

    resp = client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["job_store"] == "ok"
    assert body["checks"]["storage_dir"].startswith("error:")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_providers_models_rate_limit_blocks_after_threshold(client: TestClient):
    """11th request inside the 10/min budget gets 429."""
    body = {"provider": "openai", "api_key": "fake"}
    # Endpoint will return 400 (unknown api_key) on first 10 calls, then
    # 429 on the 11th. We don't care about the 400 — only that the
    # rate-limit kicks in at the 11th attempt.
    for _ in range(10):
        client.post("/api/providers/models", json=body)
    resp = client.post("/api/providers/models", json=body)
    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"].lower()


def test_rate_limit_uses_x_forwarded_for(monkeypatch: pytest.MonkeyPatch):
    """Roadmap L3 (R1) — slowapi keys on the real client IP, not the proxy's.

    Under HF Spaces (and any reverse-proxy deployment) every request
    reaches the app from the same upstream IP. Without
    ``ProxyHeadersMiddleware``, ``slowapi.util.get_remote_address``
    returns that single upstream IP for everyone — the per-IP budget
    becomes a global budget and any one bursty user starves everybody
    else.

    Here we send 11 requests with 11 distinct ``X-Forwarded-For``
    values: with the middleware in place, slowapi sees 11 different
    IPs and lets all of them through. Without it, the 11th would
    be rate-limited (the existing
    ``test_providers_models_rate_limit_blocks_after_threshold``
    proves the 10/min cap is enforced).
    """
    # TRUSTED_PROXIES must be set BEFORE create_app() so the proxy
    # middleware reads the right value at app construction time.
    monkeypatch.setenv("TRUSTED_PROXIES", "*")
    app = create_app()
    app.state.limiter.reset()

    body = {"provider": "openai", "api_key": "fake"}
    with TestClient(app) as c:
        for i in range(11):
            ip = f"192.0.2.{i + 1}"  # 192.0.2.0/24 is TEST-NET-1, safe to use
            resp = c.post(
                "/api/providers/models",
                json=body,
                headers={"X-Forwarded-For": ip},
            )
            assert resp.status_code != 429, (
                f"request #{i + 1} from {ip!r} was rate-limited even though "
                f"each request claims a distinct X-Forwarded-For — proxy "
                f"headers are not being honoured"
            )


def test_create_job_endpoint_has_rate_limit_attached():
    """The @limiter.limit decorator on POST /api/jobs is registered.

    A full end-to-end check (21st request → 429) would require 20
    valid multipart uploads to exhaust the bucket — slowapi's
    decorator only counts after FastAPI body validation passes. We
    sanity-check the decorator's presence instead; the providers/models
    test above proves the limiter+middleware wiring works end-to-end.
    """
    from app.api.jobs import create_job

    # slowapi tags decorated routes with this attribute.
    assert (
        hasattr(create_job, "__wrapped__")
        or any("limit" in str(d) for d in getattr(create_job, "__slowapi_limits__", []))
        or "limit" in str(create_job)
    )
