"""P2-1 / P2-10 / P2-11 / P2-12 — API hygiene fixes.

P2-1: the SPA catch-all returned index.html (or {"status":"ok"}) with a
200 for ANY unknown URL — a typo like /api/job/123 (singular) looked
like success, masking deployment errors and fooling probes.
P2-10: every provider call opened a fresh httpx.AsyncClient (no pooling,
one TLS handshake per chunk) — now one shared pooled client.
P2-11: every model-listing failure was flattened to "400 Provider
error" — auth, rate-limit, upstream outage and timeout now map to
401/429/502/504.
P2-12: images were fully buffered via read_bytes() — now streamed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas import ModelInfo

# ---------------------------------------------------------------------------
# P2-1 — SPA catch-all
# ---------------------------------------------------------------------------


@pytest.fixture()
def plain_client():
    from app.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def test_unknown_api_path_is_404_not_200(plain_client):
    r = plain_client.get("/api/job/123")  # typo: singular
    assert r.status_code == 404


def test_unknown_api_root_is_404(plain_client):
    r = plain_client.get("/api/nope")
    assert r.status_code == 404


def test_health_subpaths_not_swallowed(plain_client):
    assert plain_client.get("/health/nope").status_code == 404


def test_health_still_works(plain_client):
    assert plain_client.get("/health").status_code == 200


def test_spa_fallback_still_serves_root(plain_client):
    # Non-API paths keep the SPA behaviour (index.html or ok-fallback).
    assert plain_client.get("/some/frontend/route").status_code == 200


# ---------------------------------------------------------------------------
# P2-10 — shared provider HTTP client
# ---------------------------------------------------------------------------


def test_shared_client_is_reused_and_recreated_after_close():
    import asyncio

    from app.providers import base as base_module

    async def scenario():
        c1 = base_module.get_shared_client()
        c2 = base_module.get_shared_client()
        assert c1 is c2  # pooled, not per-call
        await base_module.aclose_shared_client()
        c3 = base_module.get_shared_client()
        assert c3 is not c1 and not c3.is_closed
        await base_module.aclose_shared_client()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# P2-11 — provider error status mapping on /api/providers/models
# ---------------------------------------------------------------------------


class _FailingProvider:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        raise self._exc

    async def complete_structured(self, **_):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture()
def failing_client_factory():
    from app import providers as prov_module
    from app.main import create_app

    orig = prov_module._REGISTRY.copy()
    clients = []

    def make(exc: Exception) -> TestClient:
        for k in list(prov_module._REGISTRY.keys()):
            prov_module._REGISTRY[k] = _FailingProvider(exc)
        c = TestClient(create_app(), raise_server_exceptions=False)
        clients.append(c.__enter__())
        return clients[-1]

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    prov_module._REGISTRY.update(orig)


def _list_models(client: TestClient) -> int:
    r = client.post(
        "/api/providers/models",
        json={"provider": "openai", "api_key": "sk-test-123456789"},
    )
    return r.status_code


def test_permanent_401_maps_to_401(failing_client_factory):
    from corrigenda.core.protocols import ProviderPermanentError

    c = failing_client_factory(ProviderPermanentError("bad key", status_code=401))
    assert _list_models(c) == 401


def test_rate_limit_maps_to_429(failing_client_factory):
    from corrigenda.core.protocols import ProviderTransientError

    c = failing_client_factory(ProviderTransientError("slow down", status_code=429))
    assert _list_models(c) == 429


def test_upstream_5xx_maps_to_502(failing_client_factory):
    from corrigenda.core.protocols import ProviderTransientError

    c = failing_client_factory(ProviderTransientError("boom", status_code=503))
    assert _list_models(c) == 502


def test_transport_timeout_maps_to_504(failing_client_factory):
    from corrigenda.core.protocols import ProviderTransientError

    c = failing_client_factory(ProviderTransientError("read timeout"))
    assert _list_models(c) == 504


def test_unknown_error_stays_400(failing_client_factory):
    c = failing_client_factory(ValueError("weird"))
    assert _list_models(c) == 400
