"""Plan V1.3 — the frontend promise must be probeable.

The historical wrong-COPY regression (see root ``Dockerfile``) kept
``/health`` returning 200 while the SPA was gone: ``/`` served the
JSON fallback with a 200 and every probe stayed green. These tests pin
the fix: a deployment that PROMISES the frontend (``SERVE_FRONTEND=1``)
answers 503 on ``/`` and ``/health/ready`` when ``index.html`` is
missing, while backend-only deployments keep the old behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def make_client(monkeypatch):
    """Build a TestClient after env/static monkeypatching is done."""

    def _make() -> TestClient:
        app = create_app()
        app.state.limiter.reset()
        return TestClient(app, raise_server_exceptions=False)

    return _make


def _point_index_html(monkeypatch, path: Path) -> None:
    """Repoint the module-level INDEX_HTML bindings (resolved at import)."""
    monkeypatch.setattr("app.frontend_static.INDEX_HTML", path)
    monkeypatch.setattr("app.main._INDEX_HTML", path)
    monkeypatch.setattr("app.api.health.INDEX_HTML", path)


# ---------------------------------------------------------------------------
# Backend-only deployment (SERVE_FRONTEND unset) — legacy behaviour kept
# ---------------------------------------------------------------------------


def test_root_stays_ok_when_no_frontend_is_promised(make_client, monkeypatch, tmp_path):
    monkeypatch.delenv("SERVE_FRONTEND", raising=False)
    _point_index_html(monkeypatch, tmp_path / "missing" / "index.html")
    with make_client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# SPA promised (SERVE_FRONTEND=1) + index.html missing → loudly broken
# ---------------------------------------------------------------------------


def test_root_returns_503_when_promised_frontend_is_missing(make_client, monkeypatch, tmp_path):
    monkeypatch.setenv("SERVE_FRONTEND", "1")
    _point_index_html(monkeypatch, tmp_path / "missing" / "index.html")
    with make_client() as client:
        r = client.get("/")
        assert r.status_code == 503
        assert "frontend" in r.json()["detail"]
        # The SPA catch-all must be just as loud.
        assert client.get("/some/frontend/route").status_code == 503


def test_health_ready_fails_when_promised_frontend_is_missing(
    make_client, monkeypatch, tmp_path
):
    monkeypatch.setenv("SERVE_FRONTEND", "1")
    monkeypatch.setenv("JOB_STORAGE_DIR", str(tmp_path))  # storage check green
    _point_index_html(monkeypatch, tmp_path / "missing" / "index.html")
    with make_client() as client:
        r = client.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["frontend"] == "error: index.html missing"
        # Liveness and the legacy ping stay green — the PROCESS is fine.
        assert client.get("/health/live").status_code == 200
        assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# SPA promised + index.html present → everything green
# ---------------------------------------------------------------------------


def test_root_serves_spa_and_ready_is_green_when_frontend_present(
    make_client, monkeypatch, tmp_path
):
    monkeypatch.setenv("SERVE_FRONTEND", "1")
    monkeypatch.setenv("JOB_STORAGE_DIR", str(tmp_path))
    index = tmp_path / "index.html"
    index.write_text('<!doctype html><html><body><div id="root"></div></body></html>')
    _point_index_html(monkeypatch, index)
    with make_client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert '<div id="root">' in r.text

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["frontend"] == "ok"
