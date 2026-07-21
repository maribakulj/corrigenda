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
    # Plan V2.1 — create_job became a thin slot-reservation wrapper; the
    # offload-sensitive body lives in _create_job_reserved.
    src = Path("app/api/jobs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_create_job_reserved":
            return node
    raise AssertionError("_create_job_reserved not found")


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
    resp = client.get(f"/api/jobs/{job_id}/download", headers={"X-Job-Token": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")


# ---------------------------------------------------------------------------
# Wave-3 adversarial-review follow-ups.
# ---------------------------------------------------------------------------

# Review finding 1 (MAJOR, security) — the F22 repr fallback stringified
# extras with bare repr() AFTER the RedactionFilter ran, so a secret
# inside a non-serialisable object's repr reached the JSON log verbatim;
# dict-valued extras carried secrets through untouched too.


def _formatted_record_with_extra(value: object) -> str:
    import logging as _logging

    from app.observability.logging_config import JsonFormatter, RedactionFilter

    record = _logging.LogRecord(
        name="test",
        level=_logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="provider call failed",
        args=None,
        exc_info=None,
    )
    record.ctx = value
    # Same order as the real handler: filter first, then formatter.
    assert RedactionFilter().filter(record)
    return JsonFormatter().format(record)


def test_review_w3_repr_fallback_is_sanitized():
    class _Ctx:
        def __repr__(self) -> str:
            return "<ProviderCtx api_key=sk-verysecret12345 retries=2>"

        def __reduce__(self):  # make json.dumps fail for sure
            raise TypeError

    out = _formatted_record_with_extra(_Ctx())
    assert "sk-verysecret12345" not in out, out
    assert "ctx" in out  # the extra itself is still logged (masked)


def test_review_w3_dict_extra_is_sanitized():
    out = _formatted_record_with_extra({"api_key": "sk-verysecret12345", "n": 3})
    assert "sk-verysecret12345" not in out, out
    assert '"n": 3' in out  # non-secret leaves untouched


def test_review_w3_nested_list_extra_is_sanitized():
    out = _formatted_record_with_extra(["ok", {"auth": "Bearer sk-verysecret12345"}])
    assert "sk-verysecret12345" not in out, out


# Review finding 2 (MAJOR) — /api/providers/models takes a JSON body but
# sat outside the F18 guard: a chunked-TE request streaming forever
# accumulates in memory via await request.body() (single-worker OOM).


def test_review_w3_models_endpoint_missing_content_length_rejected(client):
    def _chunked():
        yield b'{"provider": "openai", '
        yield b'"api_key": "k"}'

    resp = client.post(
        "/api/providers/models",
        content=_chunked(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.text


def test_review_w3_models_endpoint_oversized_body_rejected(client):
    big = b'{"provider": "openai", "api_key": "' + b"k" * (2 * 1024 * 1024) + b'"}'
    resp = client.post(
        "/api/providers/models",
        content=big,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.text


def test_review_w3_models_endpoint_normal_body_still_reaches_handler(client):
    # Small well-formed body: the guard must NOT 413 it (the handler then
    # fails on the fake key upstream, which is fine — anything but 413).
    resp = client.post(
        "/api/providers/models",
        json={"provider": "openai", "api_key": "k"},
    )
    assert resp.status_code != 413, resp.text


# Review finding 3 — the create_job rollback ran store.delete_job (an
# rmtree over a possibly multi-hundred-MB extraction) inline on the loop.
# Review finding 5 — the AST helper counted ANY to_thread call, awaited
# or not, anywhere: it is now awaited-only, and the ZIP test names the
# helper instead of accepting any offload.


def _awaited_to_thread_names(node: ast.AST) -> set[str]:
    """Names of callables passed to an AWAITED asyncio.to_thread(...)."""
    offloaded: set[str] = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Await):
            continue
        # Unwrap asyncio.shield(asyncio.to_thread(...)) — shielding a
        # rollback offload is still an offload.
        call = n.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "shield"
            and call.args
        ):
            call = call.args[0]
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "to_thread"
            and call.args
        ):
            first = call.args[0]
            if isinstance(first, ast.Name):
                offloaded.add(first.id)
            elif isinstance(first, ast.Attribute):
                offloaded.add(first.attr)
    return offloaded


def test_review_w3_rollback_delete_job_is_offloaded():
    assert "delete_job" in _awaited_to_thread_names(_create_job_source())


def test_review_w3_offloads_are_awaited_not_just_present():
    offloaded = _awaited_to_thread_names(_create_job_source())
    assert {"save_uploaded_files", "build_document_manifest", "create_job"} <= offloaded


def test_review_w3_zip_build_offload_is_named():
    assert "_build_zip_archive" in _awaited_to_thread_names(_download_source())


# Review finding 6 — the /diff, /layout and /trace projections are
# CPU-bound walks over full document manifests (up to a 200 MiB corpus),
# left inline in async handlers.


def _handler_source(name: str) -> ast.AST:
    import inspect
    import textwrap

    from app.api import jobs as jobs_module

    fn = getattr(jobs_module, name)
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def test_review_w3_read_model_projections_are_offloaded():
    assert "build_diff" in _awaited_to_thread_names(_handler_source("get_job_diff"))
    assert "build_layout" in _awaited_to_thread_names(_handler_source("get_job_layout"))
    assert "model_dump" in _awaited_to_thread_names(_handler_source("get_job_trace"))


# Review finding 4 — a job EVICTED mid-stream fell through the keepalive
# re-check (job None) and keepalived forever; an evicted job is terminal
# by definition, so the stream must end with a job_not_found error.


async def test_review_w3_evicted_job_stream_terminates(monkeypatch, tmp_path):
    import asyncio

    from app import storage as storage_module
    from app.jobs.store import JobStore

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    store = JobStore()
    monkeypatch.setattr(JobStore, "KEEPALIVE_TIMEOUT_SECONDS", 0.05)
    job_id = store.create_job(Provider.OPENAI, "m")

    received: list[str] = []

    async def _consume():
        async for ev in store.stream_events(job_id):
            received.append(ev.event if isinstance(ev.event, str) else ev.event.value)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.02)  # subscribed, inside the poll loop
    store.delete_job(job_id)  # evicted mid-stream
    await asyncio.wait_for(consumer, timeout=2.0)  # pre-fix: hangs forever

    assert received, "stream ended with no events"
    assert received[-1] == "error"


# ---------------------------------------------------------------------------
# Cumulative-review follow-ups (wave 6).
# ---------------------------------------------------------------------------


async def test_review_w6_evicted_stream_delivers_buffered_terminal(monkeypatch, tmp_path):
    """Cumulative review finding 1 — the eviction branch yielded
    job_not_found WITHOUT draining the subscriber's queue first, so a real
    terminal event already buffered when the job is evicted (emit landed it,
    then delete_job ran) would be silently replaced by a generic error.
    The buffered terminal must be delivered; only an empty queue yields
    job_not_found.

    We reproduce the exact race deterministically: the keepalive timeout
    fires, and at the branch's `self._jobs.get(job_id)` we inject a
    terminal into this subscriber's queue AND report the job gone."""
    import asyncio

    from app import storage as storage_module
    from app.jobs.events import JobEventType
    from app.jobs.store import JobStore, SSEEvent

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    store = JobStore()
    monkeypatch.setattr(JobStore, "KEEPALIVE_TIMEOUT_SECONDS", 0.05)
    job_id = store.create_job(Provider.OPENAI, "m")

    injected = {"done": False}

    class _RacingJobs(dict):
        def get(self, jid, default=None):
            # First lookup inside the timeout branch: simulate
            # emit(completed) landing in the queue AND delete_job evicting
            # the job, exactly between the timeout and the branch's status
            # check — the precise race the drain must survive.
            if jid == job_id and not injected["done"]:
                injected["done"] = True
                queues = store._subscribers.get(job_id, [])
                if queues:
                    queues[0].put_nowait(
                        SSEEvent(
                            event=JobEventType.COMPLETED,
                            data={"total_lines": 7},
                        )
                    )
                return None  # job evicted
            return super().get(jid, default)

    received: list = []

    async def _consume():
        async for ev in store.stream_events(job_id):
            received.append(ev)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.02)  # subscribed, in the poll loop, queue empty
    store._jobs = _RacingJobs(store._jobs)  # arm the race for the next lookup
    await asyncio.wait_for(consumer, timeout=2.0)

    names = [ev.event if isinstance(ev.event, str) else ev.event.value for ev in received]
    # The buffered terminal is delivered; NO job_not_found error masks it.
    assert "completed" in names, names
    assert names[-1] == "completed", names
    assert not any(
        isinstance(ev.data, dict) and ev.data.get("reason") == "job_not_found" for ev in received
    ), names


def test_review_w6_sanitize_deep_covers_sets_and_objects():
    """Cumulative review finding 2 — _sanitize_deep's isinstance ladder
    handled str/dict/list/tuple only; set/frozenset and arbitrary objects
    fell through UNSANITIZED on the record (redaction then depended on
    JsonFormatter's json.dumps incidentally failing). The filter must
    sanitise those leaves itself."""
    from app.observability.logging_config import _sanitize_deep

    def fake_sanitize(s: str) -> str:
        return s.replace("sk-verysecret12345", "sk-****")

    # A set leaf carrying a secret string.
    out_set = _sanitize_deep({"toks": {"Bearer sk-verysecret12345"}}, fake_sanitize)
    flat = repr(out_set)
    assert "sk-verysecret12345" not in flat, flat

    # A frozenset nested in a list.
    out_fz = _sanitize_deep(["x", frozenset({"sk-verysecret12345"})], fake_sanitize)
    assert "sk-verysecret12345" not in repr(out_fz), out_fz

    # An arbitrary object whose repr carries a secret.
    class _Ctx:
        def __repr__(self) -> str:
            return "<Ctx key=sk-verysecret12345>"

    out_obj = _sanitize_deep({"ctx": _Ctx()}, fake_sanitize)
    assert "sk-verysecret12345" not in repr(out_obj), out_obj

    # Json-safe scalars pass through untouched (not repr-ified).
    assert _sanitize_deep({"n": 3, "ok": True, "z": None}, fake_sanitize) == {
        "n": 3,
        "ok": True,
        "z": None,
    }
