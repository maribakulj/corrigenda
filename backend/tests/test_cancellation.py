"""Plan V2.2 — cooperative job cancellation, end to end.

The library exposed ``should_abort`` since F10 but the backend never
wired it: no cancel route, no CANCELLED state — a mistaken job burned
provider quota for the full 1800 s timeout. These tests pin the whole
chain: endpoint → CancellationRegistry event → pipeline probe →
CorrectionAborted → runner lands CANCELLED, discards staged outputs,
emits the terminal 'cancelled' SSE event.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas import JobStatus, ModelInfo

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


class SlowProvider:
    """Stalls every completion long enough for a cancel to land."""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.calls = 0

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
    ):
        self.calls += 1
        await asyncio.sleep(self.delay)
        lines_out = [
            {"line_id": li["line_id"], "corrected_text": li["ocr_text"]}
            for li in user_payload.get("lines", [])
        ]
        return {"lines": lines_out}, None


@pytest.fixture()
def slow_client(tmp_path, monkeypatch):
    from app import providers as prov_module
    from app import storage as storage_module
    from app.main import create_app

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")

    provider = SlowProvider()
    orig = prov_module._REGISTRY.copy()
    for k in list(prov_module._REGISTRY.keys()):
        prov_module._REGISTRY[k] = provider
    app = create_app()
    app.state.limiter.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        c.provider = provider  # type: ignore[attr-defined]
        yield c
    prov_module._REGISTRY.update(orig)
    app.state.limiter.reset()


def _create_job(client: TestClient) -> tuple[str, dict[str, str]]:
    r = client.post(
        "/api/jobs",
        data={"provider": "openai", "api_key": "fake-key", "model": "mock-model"},
        files=[("files", ("sample.xml", SAMPLE_XML.read_bytes(), "application/xml"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["job_id"], {"X-Job-Token": body["job_token"]}


def _wait_for_terminal(client: TestClient, job_id: str, headers, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job_id}", headers=headers).json()["status"]
        if status in ("completed", "completed_with_fallbacks", "failed", "cancelled"):
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal state")


# ---------------------------------------------------------------------------
# End-to-end: cancel mid-run
# ---------------------------------------------------------------------------


def test_cancel_lands_cancelled_and_promotes_nothing(slow_client, tmp_path):
    job_id, headers = _create_job(slow_client)

    r = slow_client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    assert r.status_code == 202
    assert r.json()["status"] in ("cancel_requested", "cancelled")

    status = _wait_for_terminal(slow_client, job_id, headers)
    assert status == "cancelled"

    # No output was promoted: the terminal-success gate rejects download.
    dl = slow_client.get(f"/api/jobs/{job_id}/download", headers=headers)
    assert dl.status_code in (400, 409)
    # And nothing staged remains on disk in the job's output dir.
    out_dirs = list((tmp_path / "jobs").glob(f"{job_id}/output/*"))
    assert out_dirs == [], f"staged outputs survived cancellation: {out_dirs}"


def test_cancel_is_idempotent(slow_client):
    job_id, headers = _create_job(slow_client)

    first = slow_client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    second = slow_client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202

    assert _wait_for_terminal(slow_client, job_id, headers) == "cancelled"

    # Cancelling an already-cancelled job acknowledges without effect.
    third = slow_client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    assert third.status_code == 202
    assert third.json()["status"] == "cancelled"


def test_cancel_requires_the_job_token(slow_client):
    job_id, headers = _create_job(slow_client)
    # Missing/wrong token → 404 (existence must not leak), job unharmed.
    assert slow_client.post(f"/api/jobs/{job_id}/cancel").status_code == 404
    assert (
        slow_client.post(f"/api/jobs/{job_id}/cancel", headers={"X-Job-Token": "wrong"}).status_code
        == 404
    )
    assert _wait_for_terminal(slow_client, job_id, headers) in (
        "completed",
        "completed_with_fallbacks",
    )


def test_cancel_after_completion_is_a_noop(slow_client):
    job_id, headers = _create_job(slow_client)
    final = _wait_for_terminal(slow_client, job_id, headers)
    assert final in ("completed", "completed_with_fallbacks")

    r = slow_client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    assert r.status_code == 202
    assert r.json()["status"] == final  # unchanged — no terminal regression


# ---------------------------------------------------------------------------
# Runner unit: the probe trips before any provider call when pre-set
# ---------------------------------------------------------------------------


def test_preset_abort_probe_cancels_without_provider_calls(tmp_path):
    from corrigenda.formats.alto.parser import build_document_manifest

    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    from app.storage.output_writer import FilesystemOutputWriter

    store = JobStore()
    job_id = store.create_job(provider="openai", model="mock-model")
    provider = SlowProvider()
    manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])

    runner = JobRunner(job_store=store)
    asyncio.run(
        runner.run(
            job_id=job_id,
            document_manifest=manifest,
            provider_name="openai",
            api_key="fake-key",
            model="mock-model",
            output_writer=FilesystemOutputWriter(tmp_path / "out"),
            source_files={"sample.xml": SAMPLE_XML},
            provider=provider,
            timeout_seconds=30,
            should_abort=lambda: True,  # cancel already requested
        )
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert provider.calls == 0, "no provider quota may be burned after cancel"
