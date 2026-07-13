"""E2E fixtures: two mock vendor servers + one REAL backend server.

All heavy lifting (mock apps, uvicorn threading, HTTP helpers) lives in
``tests/e2e/_harness.py``; this file only wires it into pytest.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e._harness import (
    UvicornThread,
    build_absorption_only_app,
    build_honest_app,
    build_sabotage_app,
)

# Module scope (not session): the servers and the _BASE_DIR patch are
# torn down as soon as the e2e module finishes, so a bare `pytest` run's
# subsequent unit tests never execute against patched global state.


@pytest.fixture(autouse=True)
def _reset_sse_starlette_app_status() -> Iterator[None]:
    """sse-starlette keeps a process-global ``AppStatus.should_exit_event``
    bound to the FIRST event loop that serves an EventSourceResponse.
    The e2e uvicorn thread's loop claims it; a later TestClient-based
    unit test then awaits a foreign-loop event and gets an empty SSE
    body. Reset it after every e2e test so each subsequent response
    re-binds to its own loop."""
    yield
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None


@pytest.fixture(scope="module")
def honest_vendor() -> Iterator[UvicornThread]:
    srv = UvicornThread(build_honest_app())
    try:
        srv.start()
        yield srv
    finally:
        srv.stop()


@pytest.fixture(scope="module")
def sabotage_vendor() -> Iterator[UvicornThread]:
    srv = UvicornThread(build_sabotage_app())
    try:
        srv.start()
        yield srv
    finally:
        srv.stop()


@pytest.fixture(scope="module")
def absorption_vendor() -> Iterator[UvicornThread]:
    srv = UvicornThread(build_absorption_only_app())
    try:
        srv.start()
        yield srv
    finally:
        srv.stop()


@pytest.fixture(scope="module")
def backend_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[UvicornThread]:
    """The REAL backend app under uvicorn, with isolated job storage."""
    from app import storage as storage_module
    from app.main import create_app

    mp = pytest.MonkeyPatch()
    mp.setattr(storage_module, "_BASE_DIR", tmp_path_factory.mktemp("e2e-jobs"))
    srv = UvicornThread(create_app())
    try:
        srv.start()
        yield srv
    finally:
        srv.stop()
        mp.undo()
        # Final safety net for the process-global sse-starlette state
        # (see _reset_sse_starlette_app_status).
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit_event = None


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


@pytest.fixture()
def use_absorption_vendor(absorption_vendor: UvicornThread) -> Iterator[str]:
    """Repoint the Mistral provider at the absorption-only saboteur."""
    from app.providers import mistral_provider

    original = mistral_provider._BASE
    mistral_provider._BASE = absorption_vendor.base_url
    yield "mock-absorption"
    mistral_provider._BASE = original
