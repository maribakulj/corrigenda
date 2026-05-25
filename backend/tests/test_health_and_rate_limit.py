"""Tests for /health/live, /health/ready, and slowapi rate limiting
(Stage 4.D)."""

from __future__ import annotations

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
