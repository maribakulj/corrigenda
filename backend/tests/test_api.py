"""Tests for FastAPI routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas import ModelInfo, Provider

# ---------------------------------------------------------------------------
# Sample file path
# ---------------------------------------------------------------------------

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# MockProvider (same as test_orchestrator, local copy)
# ---------------------------------------------------------------------------


class MockProvider:
    async def list_models(self, api_key: str) -> list[ModelInfo]:
        return [ModelInfo(id="mock-model", label="Mock Model")]

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        lines_out = []
        for line_in in user_payload.get("lines", []):
            lines_out.append(
                {
                    "line_id": line_in["line_id"],
                    "corrected_text": line_in["ocr_text"],
                }
            )
        return {"lines": lines_out}


class BadKeyProvider:
    """Always raises on list_models."""

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        raise ValueError("Invalid API key")

    async def complete_structured(self, *args, **kwargs) -> dict[str, Any]:
        raise ValueError("Invalid API key")


class _KeyLeakingProvider:
    """Raises an exception whose message embeds the api_key — simulates
    `httpx.HTTPStatusError` repr leaking a URL like
    `...?key=AIzaSy...`. Used to verify the API handler sanitises
    provider error messages before echoing them in the HTTP response
    (L10/F1)."""

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        raise ValueError(
            f"upstream 400 at https://api.example/v1/models?key={api_key}: "
            f"unauthorized; auth header was Bearer {api_key}"
        )

    async def complete_structured(self, *args, **kwargs) -> dict[str, Any]:
        raise ValueError("not used in these tests")


# ---------------------------------------------------------------------------
# App fixture with patched provider registry
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """TestClient with MockProvider injected into the provider registry."""
    from app import providers as prov_module
    from app.main import create_app

    mock = MockProvider()
    orig_registry = prov_module._REGISTRY.copy()
    # Patch all providers to MockProvider so no real API calls happen
    for k in list(prov_module._REGISTRY.keys()):
        prov_module._REGISTRY[k] = mock

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    prov_module._REGISTRY.update(orig_registry)


@pytest.fixture()
def bad_key_client():
    """TestClient with BadKeyProvider."""
    from app import providers as prov_module
    from app.main import create_app

    bad = BadKeyProvider()
    orig_registry = prov_module._REGISTRY.copy()
    for k in list(prov_module._REGISTRY.keys()):
        prov_module._REGISTRY[k] = bad

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    prov_module._REGISTRY.update(orig_registry)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sample_xml_upload(filename: str = "sample.xml"):
    return ("files", (filename, SAMPLE_XML.read_bytes(), "application/xml"))


def _form_fields(provider: str = "openai") -> dict:
    return {
        "provider": provider,
        "api_key": "fake-key",
        "model": "mock-model",
    }


# ---------------------------------------------------------------------------
# test_list_models_invalid_provider
# ---------------------------------------------------------------------------


def test_list_models_invalid_provider(client: TestClient):
    resp = client.post(
        "/api/providers/models",
        json={"provider": "nonexistent_llm", "api_key": "x"},
    )
    assert resp.status_code == 422  # Pydantic validation error (invalid enum)


# ---------------------------------------------------------------------------
# test_list_models_bad_api_key
# ---------------------------------------------------------------------------


def test_list_models_bad_api_key(bad_key_client: TestClient):
    resp = bad_key_client.post(
        "/api/providers/models",
        json={"provider": "openai", "api_key": "bad-key"},
    )
    assert resp.status_code == 400
    assert "Provider error" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# L10/F1 — provider error messages must be sanitised before being echoed
# in the HTTP response. A provider that lands the api_key in its
# exception string (URL params, Authorization header repr, etc.) must
# NOT cause the key to surface in the response body.
# ---------------------------------------------------------------------------


def test_list_models_response_does_not_echo_api_key_on_provider_error():
    """L10/F1 — pre-fix `/api/providers/models` returned
    `detail=f"Provider error ({provider}): {exc}"`. If a provider
    raised an exception whose string repr embedded the api_key (as
    httpx.HTTPStatusError does for keys passed via URL params), the
    key landed in the HTTP response and the operator logs.

    This test injects a deliberately leaky provider whose exception
    message contains the api_key in multiple shapes (URL `?key=...`
    and `Bearer ...` header). The handler must sanitise via
    `alto_core.sanitize_error` before echoing — the key must not
    appear anywhere in the response.
    """
    from app import providers as prov_module
    from app.main import create_app

    leaker = _KeyLeakingProvider()
    orig_registry = prov_module._REGISTRY.copy()
    for k in list(prov_module._REGISTRY.keys()):
        prov_module._REGISTRY[k] = leaker

    try:
        app = create_app()
        app.state.limiter.reset()
        with TestClient(app, raise_server_exceptions=False) as c:
            secret = "AIzaSyD-LEAKY-PROVIDER-SECRET-VALUE-1234"
            resp = c.post(
                "/api/providers/models",
                json={"provider": "google", "api_key": secret},
            )

        assert resp.status_code == 400
        body_text = resp.text
        assert secret not in body_text, (
            f"api_key leaked in /api/providers/models response: {body_text!r}. "
            f"Handler must call alto_core.sanitize_error(str(exc), api_key=body.api_key)."
        )
        # The redacted prefix (first 4 chars + "****") IS expected to
        # appear, confirming sanitize_error actually ran rather than
        # the exception being suppressed.
        assert "****" in body_text or "Bearer" not in body_text, (
            f"sanitize_error doesn't appear to have run; response: {body_text!r}"
        )
    finally:
        prov_module._REGISTRY.update(orig_registry)
        app.state.limiter.reset()


# ---------------------------------------------------------------------------
# test_create_job_no_files
# ---------------------------------------------------------------------------


def test_create_job_no_files(client: TestClient):
    resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[],
    )
    # 422 (no files provided) or 400 (no ALTO found)
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# test_create_job_invalid_extension
# ---------------------------------------------------------------------------


def test_create_job_invalid_extension(client: TestClient):
    resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[("files", ("doc.pdf", b"%PDF-1.4", "application/pdf"))],
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


def test_create_job_rejects_file_exceeding_upload_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """L10/B5 — `POST /api/jobs` previously did `await f.read()` with
    no size cap, loading the entire upload into memory before any
    decoding. A 100 GB raw XML upload could OOM the single-worker
    server.

    After the fix the route bounds each file at `_MAX_UPLOAD_FILE_BYTES`
    bytes and returns 413 (Payload Too Large) immediately, without
    reading more than the cap into memory. Test monkey-patches the
    cap down to 100 bytes and sends a 200-byte file.
    """
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILE_BYTES", 100)

    big_payload = b"<?xml version='1.0'?>" + b"x" * 200
    resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[("files", ("big.xml", big_payload, "application/xml"))],
    )
    assert resp.status_code == 413, (
        f"upload exceeding the cap must yield 413, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert "too large" in detail.lower() or "exceeds" in detail.lower(), (
        f"413 response should mention the cap; got detail={detail!r}"
    )


def test_download_multi_file_returns_valid_zip(client: TestClient):
    """L10/F8 regression net — the multi-file ZIP download path had no
    behavioural test pre-L10. This commit refactors it from
    `Response(content=BytesIO.getvalue())` (full materialisation in
    memory) to a streamed `FileResponse` (bounded memory). The
    refactor is invisible to the client — what changes is HOW the
    bytes flow, not WHAT bytes flow. This test pins the WHAT so the
    refactor cannot silently corrupt the archive."""
    import io
    import zipfile

    from app.storage import init_job_dirs, output_dir

    store = client.app.state.job_store
    job_id = store.create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    out_dir = output_dir(job_id)
    payload_a = b"<alto><A/></alto>"
    payload_b = b"<alto><B/></alto>"
    (out_dir / "a.corrected.xml").write_bytes(payload_a)
    (out_dir / "b.corrected.xml").write_bytes(payload_b)

    resp = client.get(f"/api/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    assert f"job_{job_id}_corrected.zip" in resp.headers.get("content-disposition", "")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["a.corrected.xml", "b.corrected.xml"]
        assert zf.read("a.corrected.xml") == payload_a
        assert zf.read("b.corrected.xml") == payload_b


def test_download_job_does_not_materialise_full_zip_in_memory():
    """L10/F8 source-AST contract — `download_job` must NOT use the
    pre-fix pattern `Response(content=buf.getvalue())` for the
    multi-file ZIP path. That pattern materialises the entire archive
    in memory before sending, OOM-risk on large jobs × concurrent
    downloads. Use `FileResponse` (streams from disk) or
    `StreamingResponse` (streams from a generator) instead.

    The check trips on any `Response(content=...)` whose `content`
    argument's source unparse contains `getvalue` or `BytesIO`.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "api" / "jobs.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "download_job":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if isinstance(inner.func, ast.Name) and inner.func.id == "Response":
                for kw in inner.keywords:
                    if kw.arg == "content":
                        rendered = ast.unparse(kw.value)
                        if "getvalue" in rendered or "BytesIO" in rendered:
                            offenders.append((inner.lineno, rendered[:80]))
        break

    assert not offenders, (
        f"download_job still materialises ZIP in memory: {offenders}. "
        f"Use FileResponse(tmp_path, ...) or StreamingResponse(generator, ...) "
        f"to stream the archive without holding it all in memory."
    )


def test_create_job_accepts_file_at_upload_cap_boundary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Symmetric to the rejection test — exactly-cap-sized files must
    still be accepted (rejection should only kick in past the
    threshold, not at exactly the limit)."""
    from app.api import jobs as jobs_api

    monkeypatch.setattr(jobs_api, "_MAX_UPLOAD_FILE_BYTES", 100)

    # 100 bytes exactly — must NOT be rejected for size. May still
    # fail later (no ALTO content found) but that's a different 400.
    payload = b"x" * 100
    resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[("files", ("small.xml", payload, "application/xml"))],
    )
    # Whatever the status code, it must NOT be 413.
    assert resp.status_code != 413, (
        f"file at exactly the cap was rejected with 413, but the cap "
        f"is inclusive: got {resp.status_code}, {resp.text}"
    )


# ---------------------------------------------------------------------------
# test_create_job_valid_xml
# ---------------------------------------------------------------------------


def test_create_job_valid_xml(client: TestClient):
    resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[_sample_xml_upload()],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body
    assert len(body["job_id"]) == 36  # UUID


# ---------------------------------------------------------------------------
# test_get_job_unknown
# ---------------------------------------------------------------------------


def test_get_job_unknown(client: TestClient):
    resp = client.get("/api/jobs/nonexistent-id-xyz")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# test_get_job_known
# ---------------------------------------------------------------------------


def test_get_job_known(client: TestClient):
    # Create a job first
    create_resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[_sample_xml_upload()],
    )
    assert create_resp.status_code == 200
    job_id = create_resp.json()["job_id"]

    # Poll status
    resp = client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert "status" in body


# ---------------------------------------------------------------------------
# test_download_not_ready
# ---------------------------------------------------------------------------


def test_download_not_ready(client: TestClient):
    # Create job but do NOT wait for completion
    from app.schemas import Provider

    job_id = client.app.state.job_store.create_job(Provider.OPENAI, "mock")
    resp = client.get(f"/api/jobs/{job_id}/download")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# test_download_single_xml
# ---------------------------------------------------------------------------


def test_download_single_xml(client: TestClient):
    """Complete a job synchronously then download the output XML."""
    from alto_core.alto.parser import build_document_manifest

    from app.jobs.orchestrator import run_job
    from app.storage import init_job_dirs, output_dir, save_uploaded_files

    store = client.app.state.job_store
    provider_enum = Provider.OPENAI
    job_id = store.create_job(provider_enum, "mock")
    init_job_dirs(job_id)

    saved, _ = save_uploaded_files(job_id, [(SAMPLE_XML.name, SAMPLE_XML.read_bytes())])
    doc = build_document_manifest([(p, n) for n, p in saved.items()])
    store.update_job(job_id, document_manifest=doc)

    out_dir = output_dir(job_id)

    asyncio.run(
        run_job(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="fake-key",
            model="mock",
            output_dir=out_dir,
            source_files={n: p for n, p in saved.items()},
            provider=MockProvider(),
            job_store_override=store,
        )
    )

    resp = client.get(f"/api/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    # Must be valid XML
    from lxml import etree

    etree.fromstring(resp.content)


# ---------------------------------------------------------------------------
# test_sse_endpoint_exists
# ---------------------------------------------------------------------------


def test_sse_endpoint_exists(client: TestClient):
    """SSE endpoint returns 200 and streams events; terminates if job is done."""
    from app.schemas import JobStatus

    store = client.app.state.job_store
    job_id = store.create_job(Provider.OPENAI, "mock")

    # Mark job as already completed so stream_events exits immediately
    store.update_job(job_id, status=JobStatus.COMPLETED)

    resp = client.get(f"/api/jobs/{job_id}/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    # The terminal event should appear in the body
    assert "completed" in resp.text


# ---------------------------------------------------------------------------
# Regression: app/api/jobs.py must look up _JOB_TIMEOUT_SECONDS dynamically
# (not snapshot it at import time). See REMEDIATION_STATUS.md "B-NEW-1".
# ---------------------------------------------------------------------------


def test_create_job_resolves_timeout_seconds_dynamically(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """The L6 migration moved `app/api/jobs.py` from `run_job(...)` to
    `JobRunner.run(..., timeout_seconds=N)`. A naive
    `from app.jobs.orchestrator import _JOB_TIMEOUT_SECONDS` would freeze
    the value at module import — so any later mutation of
    `app.jobs.orchestrator._JOB_TIMEOUT_SECONDS` (tests, operator hot-tune,
    a future env-driven override) would silently NOT propagate to the
    actual call site. This test pins the dynamic lookup so the regression
    cannot reappear unnoticed.
    """
    from app.jobs import orchestrator as orch
    from app.jobs.runner import JobRunner

    captured: dict[str, Any] = {}

    async def _noop_coro() -> None:
        return None

    def _fake_run(self: JobRunner, **kwargs: Any):
        # Synchronous capture happens BEFORE the coroutine is scheduled by
        # `BackgroundTaskRegistry.spawn`, so the assertion below can run
        # immediately after the HTTP response without racing the scheduler.
        captured.update(kwargs)
        return _noop_coro()

    monkeypatch.setattr(JobRunner, "run", _fake_run)
    sentinel = 4242
    monkeypatch.setattr(orch, "_JOB_TIMEOUT_SECONDS", sentinel)

    resp = client.post(
        "/api/jobs",
        data=_form_fields(),
        files=[_sample_xml_upload()],
    )
    assert resp.status_code == 200, resp.text
    assert captured.get("timeout_seconds") == sentinel, (
        f"timeout_seconds was not resolved dynamically: "
        f"got {captured.get('timeout_seconds')!r}, expected {sentinel!r}. "
        f"app/api/jobs.py likely snapshotted _JOB_TIMEOUT_SECONDS at import."
    )
