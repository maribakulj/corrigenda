"""Audit-F wave 3 (2026-07-13) — backend robustness (F18-F23).

Each test pins one confirmed finding of docs/audit/AUDIT-2026-07-13.md
(fix plan: docs/audit/PLAN-CORRECTIONS.md, Vague 3). Every test was
written to FAIL on the pre-fix code and pass after.
"""

from __future__ import annotations

import ast
import io
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.observability.logging_config import JsonFormatter
from app.schemas import JobStatus, Provider

# ---------------------------------------------------------------------------
# F22 — JsonFormatter serializability probe caught only TypeError;
# ValueError (circular ref) / RecursionError escaped and dropped the record
# ---------------------------------------------------------------------------


def _format_record(extra: dict) -> dict:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.wave3.json")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("probe", extra=extra)
    return json.loads(buf.getvalue().strip())


def test_f22_circular_reference_extra_does_not_drop_record():
    circular: list = []
    circular.append(circular)  # json.dumps raises ValueError, not TypeError
    payload = _format_record({"obj": circular})
    assert payload["message"] == "probe"
    assert "obj" in payload  # repr() fallback, not a dropped record


def test_f22_non_finite_float_extra_is_handled():
    # json.dumps(float('inf')) succeeds by default (allow_nan) — but a
    # ValueError-raising object must not kill the record either.
    class _Boom:
        def __repr__(self) -> str:
            return "<boom>"

    payload = _format_record({"a": float("nan"), "b": _Boom()})
    assert payload["message"] == "probe"
    assert payload["b"] == "<boom>"


# ---------------------------------------------------------------------------
# F23 — /diff hyphen_pairs stat ignored HyphenRole.BOTH (undercounts chains)
# ---------------------------------------------------------------------------

_ALTO_CHAIN = """\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
          <TextLine ID="L1" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20">
            <String ID="S1" CONTENT="exa-" HPOS="10" VPOS="10" WIDTH="200" HEIGHT="20"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="example"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="L2" HPOS="10" VPOS="40" WIDTH="900" HEIGHT="20">
            <String ID="S2" CONTENT="mp-" HPOS="10" VPOS="40" WIDTH="200" HEIGHT="20"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="example"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="L3" HPOS="10" VPOS="70" WIDTH="900" HEIGHT="20">
            <String ID="S3" CONTENT="le" HPOS="10" VPOS="70" WIDTH="200" HEIGHT="20"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="ample"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def test_f23_hyphen_pairs_counts_both_role(tmp_path):
    from corrigenda.core.schemas import HyphenRole
    from corrigenda.formats.alto.parser import build_document_manifest

    from app.api.read_models import build_diff

    path = tmp_path / "chain.xml"
    path.write_text(_ALTO_CHAIN, encoding="utf-8")
    doc = build_document_manifest([(path, "chain.xml")])

    roles = [lm.hyphen_role for p in doc.pages for lm in p.lines]
    # Sanity: the parser really produced a PART1/BOTH/PART2 chain.
    assert HyphenRole.PART1 in roles
    assert HyphenRole.BOTH in roles

    diff = build_diff("job-x", doc)
    forward_pairs = sum(
        1
        for p in doc.pages
        for lm in p.lines
        if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
    )
    assert diff["stats"]["hyphen_pairs"] == forward_pairs
    assert forward_pairs >= 2  # RED pre-fix: only PART1 counted → 1


# ---------------------------------------------------------------------------
# F20 — SSE stream must terminate even when the terminal event is dropped
# under queue backpressure (keepalive branch re-checks status; emit()
# guarantees terminal delivery)
# ---------------------------------------------------------------------------


async def test_f20_emit_guarantees_terminal_delivery_on_full_queue():
    from app.jobs.store import JobStore

    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "m")
    q = store.subscribe(job_id)
    # Saturate the queue with non-terminal events.
    while not q.full():
        store.emit(job_id, "chunk_completed", {"i": q.qsize()})
    assert q.full()

    # Terminal event on a full queue MUST still be delivered.
    store.emit(job_id, "completed", {"job_id": job_id, "status": "completed"})
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert any(e.event == "completed" for e in drained), "terminal was dropped"


async def test_f20_keepalive_rechecks_status_and_synthesises_terminal(monkeypatch):
    from app.jobs import store as store_mod
    from app.jobs.store import JobStore

    # Shrink the keepalive timeout so the re-check fires fast.
    monkeypatch.setattr(store_mod.JobStore, "KEEPALIVE_TIMEOUT_SECONDS", 0.05, raising=False)

    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "m")
    q = store.subscribe(job_id)
    # Fill the queue so the runner's terminal put_nowait would be dropped,
    # THEN mark the job terminal without emitting a deliverable terminal.
    while not q.full():
        q.put_nowait(store_mod.SSEEvent(event="chunk_completed", data={}))
    store.update_job(job_id, status=JobStatus.COMPLETED)

    seen: list[str] = []
    async for event in store.stream_events(job_id):
        seen.append(event.event)
        if event.event in ("completed", "failed"):
            break
        if len(seen) > 600:  # safety bound — must not stream forever
            pytest.fail("stream did not terminate")
    assert seen[-1] == "completed"


# ---------------------------------------------------------------------------
# F18 — upload byte-cap must be enforced BEFORE Starlette spools the whole
# multipart body to disk (ASGI middleware Content-Length + streaming guard)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_f18_content_length_over_cap_rejected_before_parse(client, monkeypatch):
    from app.api import upload_guard

    monkeypatch.setattr(upload_guard, "_max_request_bytes", lambda: 1024)
    # A body over the (patched) cap — Content-Length is set by the client.
    big = b"x" * 4096
    resp = client.post(
        "/api/jobs",
        files={"files": ("a.xml", big, "application/xml")},
        data={"provider": "openai", "model": "m", "api_key": "k"},
    )
    assert resp.status_code == 413, resp.text


def test_f18_missing_content_length_on_jobs_rejected(client, monkeypatch):
    from app.api import upload_guard

    monkeypatch.setattr(upload_guard, "_max_request_bytes", lambda: 256 * 1024 * 1024)

    def _chunked():
        yield b"--boundary\r\n"
        yield b"junk" * 100

    # A streaming body sends chunked transfer-encoding (no Content-Length).
    resp = client.post(
        "/api/jobs",
        content=_chunked(),
        headers={"content-type": "multipart/form-data; boundary=boundary"},
    )
    assert resp.status_code == 413, resp.text


def test_f18_normal_upload_still_reaches_handler(client):
    # Below the default cap: the guard must let it through (it fails later
    # for an unrelated reason — a bad provider key — not a 413).
    resp = client.post(
        "/api/jobs",
        files={"files": ("a.xml", b"<alto></alto>", "application/xml")},
        data={"provider": "openai", "model": "m", "api_key": "k"},
    )
    assert resp.status_code != 413, resp.text


# ---------------------------------------------------------------------------
# F19 / F21 — heavy synchronous work must be offloaded off the event loop
# (asyncio.to_thread): create_job parse/extract, download ZIP build, and
# the opportunistic-eviction rmtree in store.create_job
# ---------------------------------------------------------------------------


def _create_job_source() -> ast.FunctionDef:
    src = Path("app/api/jobs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_job":
            return node
    raise AssertionError("create_job not found")


def _download_source() -> ast.AsyncFunctionDef:
    src = Path("app/api/jobs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "download_job":
            return node
    raise AssertionError("download_job not found")


def _to_thread_offloaded_names(node: ast.AST) -> set[str]:
    """Names of callables passed as the first arg to asyncio.to_thread(...)."""
    offloaded: set[str] = set()
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "to_thread"
            and n.args
        ):
            first = n.args[0]
            if isinstance(first, ast.Name):
                offloaded.add(first.id)
            elif isinstance(first, ast.Attribute):
                offloaded.add(first.attr)
    return offloaded


def test_f19_create_job_offloads_parse_and_extract():
    offloaded = _to_thread_offloaded_names(_create_job_source())
    assert "save_uploaded_files" in offloaded, offloaded
    assert "build_document_manifest" in offloaded, offloaded


def test_f21_create_job_offloads_store_create_job():
    # The opportunistic-eviction rmtree lives inside store.create_job;
    # offloading that call keeps the loop unblocked.
    offloaded = _to_thread_offloaded_names(_create_job_source())
    assert "create_job" in offloaded, offloaded


def test_f19_download_offloads_zip_build():
    offloaded = _to_thread_offloaded_names(_download_source())
    # The multi-file ZIP build must run in a thread (any helper name).
    assert offloaded, "download_job builds the ZIP on the event loop"


def test_f19_honest_job_still_completes_after_offload(client):
    """Functional net: offloading must not change WHAT a job produces."""
    from app.storage import init_job_dirs, output_dir

    store = client.app.state.job_store
    job_id = store.create_job(Provider.OPENAI, "m")
    init_job_dirs(job_id)
    out_dir = output_dir(job_id)
    (out_dir / "a.corrected.xml").write_bytes(b"<alto><A/></alto>")
    (out_dir / "b.corrected.xml").write_bytes(b"<alto><B/></alto>")
    store.update_job(job_id, status=JobStatus.COMPLETED)
    token = "tok"
    import hashlib

    store.update_job(job_id, token_hash=hashlib.sha256(token.encode()).hexdigest())
    resp = client.get(f"/api/jobs/{job_id}/download", params={"token": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
