"""E2E fixtures: two mock vendor servers + one REAL backend server.

All heavy lifting (mock apps, uvicorn threading, HTTP helpers) lives in
``tests/e2e/_harness.py``; this file only wires it into pytest.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e._harness import UvicornThread, build_honest_app, build_sabotage_app


@pytest.fixture(scope="session")
def honest_vendor() -> Iterator[UvicornThread]:
    srv = UvicornThread(build_honest_app())
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def sabotage_vendor() -> Iterator[UvicornThread]:
    srv = UvicornThread(build_sabotage_app())
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def backend_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[UvicornThread]:
    """The REAL backend app under uvicorn, with isolated job storage."""
    from app import storage as storage_module
    from app.main import create_app

    original_base_dir = storage_module._BASE_DIR
    storage_module._BASE_DIR = tmp_path_factory.mktemp("e2e-jobs")
    srv = UvicornThread(create_app())
    srv.start()
    yield srv
    srv.stop()
    storage_module._BASE_DIR = original_base_dir


@pytest.fixture()
def use_honest_vendor(honest_vendor: UvicornThread) -> Iterator[str]:
    """Repoint the Mistral provider at the honest mock for this test."""
    from app.providers import mistral_provider

    original = mistral_provider._BASE
    mistral_provider._BASE = honest_vendor.base_url
    yield "mock-mistral-small"
    mistral_provider._BASE = original


@pytest.fixture()
def use_sabotage_vendor(sabotage_vendor: UvicornThread) -> Iterator[str]:
    """Repoint the Mistral provider at the saboteur mock for this test."""
    from app.providers import mistral_provider

    original = mistral_provider._BASE
    mistral_provider._BASE = sabotage_vendor.base_url
    yield "mock-sabotage"
    mistral_provider._BASE = original
