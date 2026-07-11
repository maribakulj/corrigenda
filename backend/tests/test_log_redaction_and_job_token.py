"""P1-6 — central log redaction · P1-7 — capability-token access.

P1-6: HTTP responses were sanitised, but the runner logged the RAW
exception before computing the safe message, the task registry logged
raw tracebacks, and the formatters serialised both verbatim — an API
key embedded in a provider error could reach the logs unmasked. A
handler-level RedactionFilter now redacts every record (message and
formatted traceback) before any formatter sees it.

P1-7: the job_id was the ONLY secret — a UUID that leaks into operator
logs and browser history gave full read access to a stranger's OCR
text, corrections and images. Every API-created job now carries a
capability token (hash stored server-side); a missing/wrong token gets
404 (indistinguishable from a missing job).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.observability.logging_config import JsonFormatter, RedactionFilter
from tests.test_api import MockProvider

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# P1-6 — RedactionFilter
# ---------------------------------------------------------------------------


def _format_through_pipeline(record: logging.LogRecord) -> dict:
    """Run a record through the filter + JSON formatter, as a handler does."""
    RedactionFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


def test_secret_shaped_message_is_redacted():
    rec = logging.LogRecord(
        name="app.jobs.runner",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider refused: api_key=sk-abcdef1234567890abcdef invalid",
        args=None,
        exc_info=None,
    )
    out = _format_through_pipeline(rec)
    assert "sk-abcdef1234567890abcdef" not in out["message"]


def test_traceback_containing_secret_is_redacted():
    try:
        raise RuntimeError("401 Unauthorized: Bearer sk-secret1234567890xyz")
    except RuntimeError:
        import sys

        rec = logging.LogRecord(
            name="app.jobs.task_registry",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="background task crashed",
            args=None,
            exc_info=sys.exc_info(),
        )
    out = _format_through_pipeline(rec)
    assert "sk-secret1234567890xyz" not in out.get("exception", "")
    assert "RuntimeError" in out.get("exception", "")  # traceback still useful


def test_lazy_percent_args_still_work():
    rec = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="job %s finished in %ss",
        args=("j1", 3),
        exc_info=None,
    )
    out = _format_through_pipeline(rec)
    assert out["message"] == "job j1 finished in 3s"


# ---------------------------------------------------------------------------
# P1-7 — capability token (end to end)
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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    prov_module._REGISTRY.update(orig)


def _create(client) -> tuple[str, str]:
    r = client.post(
        "/api/jobs",
        data={"provider": "openai", "api_key": "k", "model": "mock-model"},
        files=[("files", ("s.xml", SAMPLE_XML.read_bytes(), "application/xml"))],
    )
    assert r.status_code == 200
    body = r.json()
    return body["job_id"], body["job_token"]


def test_token_is_minted_and_only_its_hash_is_stored(client):
    import hashlib

    job_id, token = _create(client)
    assert token and len(token) >= 32
    job = client.app.state.job_store.get_job(job_id)
    assert job.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in (job.token_hash or "")


def test_endpoints_refuse_without_token_as_404(client):
    """Missing/wrong token is indistinguishable from a missing job."""
    job_id, token = _create(client)
    for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/download"):
        assert client.get(path).status_code == 404
        assert client.get(path, headers={"X-Job-Token": "nope"}).status_code == 404
    # With the token, the status endpoint answers.
    ok = client.get(f"/api/jobs/{job_id}", headers={"X-Job-Token": token})
    assert ok.status_code == 200


def test_token_accepted_as_query_param_for_headerless_surfaces(client):
    job_id, token = _create(client)
    r = client.get(f"/api/jobs/{job_id}?token={token}")
    assert r.status_code == 200


def test_api_responses_are_no_store(client):
    job_id, token = _create(client)
    r = client.get(f"/api/jobs/{job_id}", headers={"X-Job-Token": token})
    assert r.headers.get("cache-control") == "no-store"


def test_direct_store_jobs_stay_ungated(client):
    """Jobs created OUTSIDE the HTTP layer (tests, embedding consumers)
    have no token hash and are not gated — the security property only
    needs every PUBLICLY-created job to carry one."""
    from app.schemas import Provider

    job_id = client.app.state.job_store.create_job(Provider.OPENAI, "mock")
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
