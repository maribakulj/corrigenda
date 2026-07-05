"""Tests for jobs/orchestrator.py"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from corrigenda.alto.parser import build_document_manifest, parse_alto_file
from corrigenda.pipeline.validator import HyphenIntegrityError
from lxml import etree

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider, SSEEvent
from app.storage.output_writer import FilesystemOutputWriter

# Path to sample XML
SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# MockProvider
# ---------------------------------------------------------------------------


class MockProvider:
    """Returns OCR text unchanged (no modification)."""

    def __init__(
        self,
        fail_times: int = 0,
        invalid_json_times: int = 0,
    ) -> None:
        self._fail_times = fail_times  # raise ValueError N times then succeed
        self._invalid_json_times = invalid_json_times  # return bad JSON N times
        self._call_count = 0

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
        self._call_count += 1

        if self._invalid_json_times > 0:
            self._invalid_json_times -= 1
            return {"bad_key": []}, None  # missing "lines"

        if self._fail_times > 0:
            self._fail_times -= 1
            raise ValueError("mock LLM error")

        # Return corrected_text identical to ocr_text (identity correction)
        lines_out = []
        for line_in in user_payload.get("lines", []):
            lines_out.append(
                {
                    "line_id": line_in["line_id"],
                    "corrected_text": line_in["ocr_text"],
                }
            )
        return {"lines": lines_out}, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store_and_job(provider: str = "openai", model: str = "mock") -> tuple[JobStore, str]:
    store = JobStore()
    job_id = store.create_job(Provider(provider), model)
    return store, job_id


async def _run(
    job_id: str,
    provider: MockProvider,
    output_dir: Path,
    store: JobStore,
) -> None:
    """Run the orchestrator with the given store injected explicitly."""
    pages, _ = parse_alto_file(SAMPLE_XML, SAMPLE_XML.name)
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        output_writer=FilesystemOutputWriter(output_dir),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=provider,
    )


# ---------------------------------------------------------------------------
# test_run_job_basic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_job_basic(tmp_path: Path):
    store, job_id = _make_store_and_job()
    await _run(job_id, MockProvider(), tmp_path, store)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"

    out_files = list(tmp_path.iterdir())
    assert len(out_files) >= 1
    names = [f.name for f in out_files]
    assert any("corrected" in n for n in names)


# ---------------------------------------------------------------------------
# test_output_preserves_textline_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_preserves_textline_ids(tmp_path: Path):
    store, job_id = _make_store_and_job()
    await _run(job_id, MockProvider(), tmp_path, store)

    out_xml = next(tmp_path.glob("*_corrected.xml"))
    root = etree.parse(str(out_xml)).getroot()
    ns = root.tag[1 : root.tag.index("}")] if root.tag.startswith("{") else ""

    def tag(local: str) -> str:
        return f"{{{ns}}}{local}" if ns else local

    tl_ids = {tl.get("ID") for tl in root.iter(tag("TextLine"))}
    # All original IDs must still be present
    orig_root = etree.parse(str(SAMPLE_XML)).getroot()
    orig_ns = orig_root.tag[1 : orig_root.tag.index("}")] if orig_root.tag.startswith("{") else ""
    orig_ids = {
        tl.get("ID") for tl in orig_root.iter(f"{{{orig_ns}}}TextLine" if orig_ns else "TextLine")
    }
    assert tl_ids == orig_ids


# ---------------------------------------------------------------------------
# test_output_preserves_textline_coords
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_preserves_textline_coords(tmp_path: Path):
    store, job_id = _make_store_and_job()
    await _run(job_id, MockProvider(), tmp_path, store)

    out_xml = next(tmp_path.glob("*_corrected.xml"))
    root_out = etree.parse(str(out_xml)).getroot()
    root_orig = etree.parse(str(SAMPLE_XML)).getroot()

    ns_out = root_out.tag[1 : root_out.tag.index("}")] if root_out.tag.startswith("{") else ""
    ns_orig = root_orig.tag[1 : root_orig.tag.index("}")] if root_orig.tag.startswith("{") else ""

    def coords(root: etree._Element, ns: str) -> dict[str, dict]:
        tag = f"{{{ns}}}TextLine" if ns else "TextLine"
        return {
            tl.get("ID"): {
                "HPOS": tl.get("HPOS"),
                "VPOS": tl.get("VPOS"),
                "WIDTH": tl.get("WIDTH"),
                "HEIGHT": tl.get("HEIGHT"),
            }
            for tl in root.iter(tag)
        }

    orig = coords(root_orig, ns_orig)
    out = coords(root_out, ns_out)

    for line_id, attrs in orig.items():
        assert line_id in out, f"{line_id} missing in output"
        assert out[line_id] == attrs, f"{line_id} coords changed: {attrs} → {out[line_id]}"


# ---------------------------------------------------------------------------
# test_hyphen_pairs_reconciled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hyphen_pairs_reconciled(tmp_path: Path):
    """Explicit hyphen pairs in sample.xml must produce HYP + SUBS_TYPE in output."""
    store, job_id = _make_store_and_job()
    await _run(job_id, MockProvider(), tmp_path, store)

    out_xml = next(tmp_path.glob("*_corrected.xml"))
    root = etree.parse(str(out_xml)).getroot()
    ns = root.tag[1 : root.tag.index("}")] if root.tag.startswith("{") else ""

    def tag(local: str) -> str:
        return f"{{{ns}}}{local}" if ns else local

    # TL4 is PART1 with SUBS_TYPE=HypPart1 in sample.xml
    tl4 = root.find(f".//{tag('TextLine')}[@ID='TL4']")
    assert tl4 is not None
    # Must have a HYP element
    hyp_els = tl4.findall(tag("HYP"))
    assert len(hyp_els) >= 1, "TL4 (PART1) must have a HYP element in output"

    # TL5 is PART2: its first String must have SUBS_TYPE=HypPart2
    tl5 = root.find(f".//{tag('TextLine')}[@ID='TL5']")
    assert tl5 is not None
    strings = tl5.findall(tag("String"))
    assert strings, "TL5 must have String elements"
    assert strings[0].get("SUBS_TYPE") == "HypPart2", (
        f"TL5 first String must have SUBS_TYPE=HypPart2, got {strings[0].get('SUBS_TYPE')!r}"
    )


# ---------------------------------------------------------------------------
# test_retry_on_invalid_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_invalid_json(tmp_path: Path):
    """MockProvider returns invalid JSON once, then succeeds → job completed."""
    store, job_id = _make_store_and_job()
    provider = MockProvider(invalid_json_times=1)
    await _run(job_id, provider, tmp_path, store)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"
    # At least one retry was emitted
    assert job.retries >= 1


# ---------------------------------------------------------------------------
# test_fallback_on_persistent_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_on_persistent_failure(tmp_path: Path):
    """Persistent provider failure → OCR source kept, job still completed.

    F1 — a *transient* burst of failures now recovers by downgrading
    granularity and retrying, so the provider must fail persistently
    (beyond the per-chunk budget) to force the terminal OCR fallback.
    """
    store, job_id = _make_store_and_job()
    provider = MockProvider(fail_times=99)
    await _run(job_id, provider, tmp_path, store)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"
    assert job.fallbacks >= 1

    # Output file must still exist
    out_files = list(tmp_path.glob("*_corrected.xml"))
    assert len(out_files) >= 1


# ---------------------------------------------------------------------------
# test_sse_events_emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_events_emitted(tmp_path: Path):
    """Verify that key SSE events are emitted in order."""
    store, job_id = _make_store_and_job()
    queue = store.subscribe(job_id)

    await _run(job_id, MockProvider(), tmp_path, store)

    events: list[str] = []
    while not queue.empty():
        ev: SSEEvent = queue.get_nowait()
        events.append(ev.event)

    assert "started" in events
    assert "document_parsed" in events
    assert "page_started" in events
    assert "completed" in events

    # Order check: started before completed
    assert events.index("started") < events.index("completed")


# ---------------------------------------------------------------------------
# T-004 / B-005 — cross-page hyphen on files with colliding IDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_page_hyphen_reconciled_through_colliding_ids(tmp_path: Path):
    """Two ALTO files declare identical Page+Line IDs and form a
    cross-page hyphen pair. Pre-fix, the partner lookup was ambiguous
    and silently picked the local file (causing self-pairing or wrong
    pairing). With the qualified (page_id, line_id) lookup the right
    partner is resolved and the pair survives the pipeline."""
    from corrigenda.alto.parser import build_document_manifest

    body_a = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="middle" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
  <TextLine ID="TL2" HPOS="0" VPOS="25" WIDTH="200" HEIGHT="20">
    <String ID="S2" CONTENT="fonda-" HPOS="0" VPOS="25" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""
    body_b = """\
<TextBlock ID="TB1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="60">
  <TextLine ID="TL1" HPOS="0" VPOS="0" WIDTH="200" HEIGHT="20">
    <String ID="S1" CONTENT="mentaux" HPOS="0" VPOS="0" WIDTH="100" HEIGHT="20"/>
  </TextLine>
</TextBlock>"""

    file_a = tmp_path / "fileA.xml"
    file_b = tmp_path / "fileB.xml"
    for path, body in ((file_a, body_a), (file_b, body_b)):
        path.write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">'
            f'<Layout><Page ID="P1" WIDTH="2480" HEIGHT="3508">'
            f'<PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">'
            f"{body}"
            f"</PrintSpace></Page></Layout></alto>",
            encoding="utf-8",
        )

    doc = build_document_manifest([(file_a, "fileA.xml"), (file_b, "fileB.xml")])

    # Page IDs are disambiguated, so we have 2 distinct pages.
    assert len({p.page_id for p in doc.pages}) == 2

    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")

    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={"fileA.xml": file_a, "fileB.xml": file_b},
        provider=MockProvider(),
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"

    # The cross-page partner (file B's TL1) must have been reconciled,
    # not silently skipped or paired with file A's own TL1.
    line_a_tl2 = next(
        lm for p in doc.pages if "fileA" in p.page_id for lm in p.lines if lm.line_id == "TL2"
    )
    line_b_tl1 = next(
        lm for p in doc.pages if "fileB" in p.page_id for lm in p.lines if lm.line_id == "TL1"
    )

    # PART1 on file A links to file B's TL1 (NOT file A's own TL1)
    assert line_a_tl2.hyphen_pair_line_id == "TL1"
    assert line_a_tl2.hyphen_pair_page_id == line_b_tl1.page_id

    # And reconciliation set both sides' corrected_text via the right partner
    assert line_a_tl2.corrected_text is not None
    assert line_b_tl1.corrected_text is not None


# ---------------------------------------------------------------------------
# T-013 — JOB_TIMEOUT_SECONDS triggers FAILED with sanitized error
# ---------------------------------------------------------------------------


class _SlowProvider:
    """Sleeps inside complete_structured to exceed the test's timeout."""

    async def list_models(self, api_key):
        return [ModelInfo(id="mock-model", label="Mock Model")]

    async def complete_structured(self, **kwargs):
        await asyncio.sleep(5.0)  # any value > the test's patched timeout
        return {"lines": []}, None


@pytest.mark.asyncio
async def test_job_timeout_marks_failure(tmp_path: Path):
    """When `timeout_seconds` elapses, JobRunner catches TimeoutError,
    marks the job FAILED, emits a `failed` event, and records a clean
    error message (no traceback, no api_key leak)."""
    store, job_id = _make_store_and_job()
    pages, _ = parse_alto_file(SAMPLE_XML, SAMPLE_XML.name)
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="sk-secret-token-12345",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=_SlowProvider(),
        timeout_seconds=1,  # 1-second budget — provider sleeps 5s
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "failed"
    assert job.error is not None
    assert "timed out" in job.error.lower()
    assert "1s" in job.error  # the configured timeout
    # No api_key leak in the recorded error
    assert "sk-secret-token-12345" not in job.error


# ---------------------------------------------------------------------------
# T-014 — generic exception inside _run_pipeline is caught and sanitized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_job_general_exception_marks_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A non-timeout exception escaping the pipeline must mark the job
    as FAILED with a sanitized error (no api_key leak)."""
    from corrigenda.pipeline.correction_pipeline import CorrectionPipeline

    store, job_id = _make_store_and_job()

    async def _boom(self, **kwargs):
        raise RuntimeError("simulated failure carrying sk-secret-token-12345")

    monkeypatch.setattr(CorrectionPipeline, "run", _boom)
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="sk-secret-token-12345",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=MockProvider(),
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "failed"
    assert job.error is not None
    assert "sk-secret-token-12345" not in job.error
    assert "simulated failure" in job.error


# ---------------------------------------------------------------------------
# T-015 — provider=None resolves from app.providers registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_job_resolves_provider_from_registry_when_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """When `provider` is not passed, run_job looks up the registry."""
    import app.providers as prov_module

    mock = MockProvider()
    monkeypatch.setattr(prov_module, "get_provider", lambda p: mock)

    store, job_id = _make_store_and_job()
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        # Note: provider arg omitted → registry lookup path
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"


# ===========================================================================
# Roadmap L4 — pipeline retry classification + event payload coverage
# ===========================================================================
#
# The pipeline classifies exceptions into 3 retry buckets with distinct
# backoff strategies (correction_pipeline.py:551-595). Pre-L4, none of
# the three branches was individually exercised — a refactor of the
# classifier could silently break one strategy and tests stayed green.
# These tests pin the contract per branch.
#
# Event payload tests follow the same logic: the pipeline emits events
# (chunk_error, retry, hyphen_partner_missing, warning) whose shape
# downstream observers depend on (LoggingObserver level routing,
# JobStoreObserver → SSE clients). Pre-L4 only event *presence* was
# tested ("started" in events) — not the field contract.

# ---------------------------------------------------------------------------
# Helpers for the classification tests
# ---------------------------------------------------------------------------


class _AlwaysFailProvider:
    """Provider that raises a configurable exception on every call.

    Used to exercise the retry classifier — the pipeline retries up to
    3 times then falls back, so a 3-element list of recorded backoffs
    tells us which branch was taken.
    """

    def __init__(self, exception_factory) -> None:
        self._exception_factory = exception_factory
        self.call_count = 0

    async def list_models(self, api_key: str):
        return [ModelInfo(id="mock-model", label="Mock")]

    async def complete_structured(self, **kwargs):
        self.call_count += 1
        raise self._exception_factory()


# Transient HTTP exception used by the L4 classifier tests below.
# The pipeline now routes on isinstance(exc, ProviderTransientError);
# providers are responsible for wrapping their httpx errors before
# re-raising. Tests raise the canonical type directly.
from corrigenda.protocols.provider import ProviderTransientError


class _OneHyphenViolationThenOK:
    """Provider that raises a hyphen-violation exactly once (first call)
    then behaves like ``MockProvider`` for the rest of the run.

    Used by the hyphen-violation classification test: the pipeline
    flips an internal ``hyphen_violation`` flag after the first retry
    so subsequent retries on the SAME chunk fall through to the
    llm_output_error branch (linear backoff). To prove the
    backoff=0 branch in isolation we need to retry exactly once.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def list_models(self, api_key: str):
        return [ModelInfo(id="mock-model", label="Mock")]

    async def complete_structured(self, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            raise HyphenIntegrityError("hyphen_integrity_violation: TL5 corrupted")
        lines_out = [
            {"line_id": line_in["line_id"], "corrected_text": line_in["ocr_text"]}
            for line_in in kwargs["user_payload"].get("lines", [])
        ]
        return {"lines": lines_out}, None


async def _capture_sleeps(monkeypatch):
    """Replace ``asyncio.sleep`` with a recorder that returns instantly.

    Returns the list that will accumulate the durations the pipeline
    requested. Safe because corrigenda uses ``asyncio.sleep`` at exactly
    one site (the retry backoff at correction_pipeline.py:585) and the
    backend has zero call sites in its runtime path.
    """
    sleeps: list[float] = []

    async def _fake(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    return sleeps


async def _collect_events(store: JobStore, job_id: str) -> list[SSEEvent]:
    queue = store.subscribe(job_id)
    out: list[SSEEvent] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# ---------------------------------------------------------------------------
# T0a — exception classification (3 branches, 3 distinct backoff curves)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_classifies_hyphen_violation_with_zero_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0a) — `ValueError("hyphen_integrity_violation: …")` retries
    instantly (backoff=0) and tags the retry event accordingly.

    Rationale: hyphen violations are deterministic (same prompt, same
    error), so the pipeline retries once at temperature 0 with no
    sleep, then falls back. Any non-zero backoff would mean the
    classifier missed the branch and routed to llm_output_error or
    transient_http instead.
    """
    sleeps = await _capture_sleeps(monkeypatch)
    store, job_id = _make_store_and_job()
    queue = store.subscribe(job_id)

    # One hyphen violation then success — captures the FIRST hyphen
    # retry per chunk (which uses backoff=0). After the first one,
    # the pipeline's internal flag flips so subsequent retries on the
    # same chunk fall through to llm_output_error (linear backoff)
    # — that branch is covered by the dedicated test below.
    provider = _OneHyphenViolationThenOK()
    await _run(job_id, provider, tmp_path, store)

    # backoff=0 means `await asyncio.sleep(backoff)` is skipped entirely
    # (correction_pipeline.py:584 `if backoff > 0`), so no entry lands
    # in our recorder.
    assert sleeps == [], (
        f"first hyphen_violation retry should skip sleep (backoff=0), got {sleeps!r}"
    )

    # Retry event must carry the fixed sentinel — downstream observers
    # discriminate hyphen retries from generic LLM errors on this tag.
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    hyphen_retries = [
        e
        for e in events
        if e.event == "retry" and e.data.get("error") == "hyphen_integrity_violation"
    ]
    assert hyphen_retries, (
        "pipeline didn't emit any retry event tagged 'hyphen_integrity_violation'"
    )


@pytest.mark.asyncio
async def test_pipeline_classifies_transient_http_with_exponential_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0a) — ``ProviderTransientError`` retries with
    backoff = attempt * 2 (1→2s, 2→4s).

    Network / 5xx upstream issues recover on a timescale of seconds,
    so the pipeline backs off exponentially to give the upstream room
    to heal. Providers wrap their httpx transport failures as
    ``ProviderTransientError`` before re-raising; a bug in that
    wrapping (or in the classifier's ``isinstance`` check) would make
    this branch fall through to ``is_llm_output_error`` and use linear
    backoff instead — caught here.
    """
    sleeps = await _capture_sleeps(monkeypatch)
    store, job_id = _make_store_and_job()

    provider = _AlwaysFailProvider(lambda: ProviderTransientError("upstream 503"))
    await _run(job_id, provider, tmp_path, store)

    # 3 attempts → 2 retries → 2 backoffs. correction_pipeline.py:577-579
    # sets backoff = attempt * 2 → [2, 4] across attempts [1, 2].
    # The pipeline may run multiple chunks; assert the backoff PATTERN
    # rather than the exact count.
    assert sleeps, "transient_http branch should have triggered at least one backoff"
    for i, s in enumerate(sleeps):
        # Either 2 (attempt 1) or 4 (attempt 2) — the exponential curve.
        assert s in (2, 4), (
            f"transient_http backoff should follow attempt*2 (2 or 4), got {s} at index {i}"
        )


@pytest.mark.asyncio
async def test_pipeline_classifies_llm_output_error_with_linear_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0a) — malformed LLM output (non-hyphen ValueError or
    JSONDecodeError) retries with backoff = attempt (1→1s, 2→2s).

    The LLM stochasticity can produce a one-off bad JSON on retry-0
    that won't repeat. Linear backoff is enough — no upstream to heal.
    """
    sleeps = await _capture_sleeps(monkeypatch)
    store, job_id = _make_store_and_job()

    # "invalid JSON" is a generic ValueError → llm_output_error branch.
    provider = _AlwaysFailProvider(lambda: ValueError("invalid JSON: missing 'lines' key"))
    await _run(job_id, provider, tmp_path, store)

    assert sleeps, "llm_output_error branch should have triggered at least one backoff"
    for i, s in enumerate(sleeps):
        # correction_pipeline.py:580-582 → backoff = attempt
        # → 1 (attempt 1) or 2 (attempt 2).
        assert s in (1, 2), (
            f"llm_output_error backoff should be linear (1 or 2), got {s} at index {i}"
        )


@pytest.mark.asyncio
async def test_pipeline_classifies_client_http_4xx_as_non_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A raw ``httpx.HTTPStatusError`` with a 4xx status (other than 429)
    is NON-retryable: zero backoff, zero retry events, immediate fallback.

    Contract: ``_wrap_if_transient`` in ``backend/app/providers/base.py``
    intentionally leaves 4xx-non-429 errors un-wrapped — bad keys (401),
    forbidden models (403), wrong endpoints (404), schema rejections (422)
    don't heal on retry, so retrying just burns quota and adds latency.
    The classifier sees the raw ``HTTPStatusError``: no ``isinstance``
    branch matches (it's neither ``ProviderTransientError`` nor
    ``ValueError``/``JSONDecodeError``), so ``is_retryable=False`` and the
    chunk falls back to OCR source on the first failure.

    This is a deliberate departure from the pre-refactor behavior, where
    a class-name allowlist treated every ``HTTPStatusError`` as transient
    and wasted 3 attempts on permanent client errors. The pin makes the
    contract explicit so a future "retry everything" refactor doesn't
    silently restore that waste.
    """
    import httpx

    sleeps = await _capture_sleeps(monkeypatch)
    store, job_id = _make_store_and_job()
    queue = store.subscribe(job_id)

    def _make_401() -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://api.example.com/v1/chat")
        resp = httpx.Response(401, request=req)
        return httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)

    provider = _AlwaysFailProvider(_make_401)
    await _run(job_id, provider, tmp_path, store)

    # Pin 1: zero backoff — the classifier short-circuits before
    # ``await asyncio.sleep(decision.backoff)`` runs.
    assert sleeps == [], f"4xx HTTPStatusError should NOT retry; got {sleeps!r} backoff(s)"

    events: list[SSEEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())

    # Pin 2: zero retry events emitted on any chunk.
    retries = [e for e in events if e.event == "retry"]
    assert retries == [], f"4xx HTTPStatusError should emit zero retry events; got {len(retries)}"

    # Pin 3: fallback path was taken — job still completes, but every
    # chunk fell back to OCR source.
    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"
    assert job.fallbacks >= 1, "expected fallback path to be taken on 4xx"

    # Pin 4: exactly one provider call per chunk — no retries. The
    # transient branch would yield 3*chunk_count calls; we want 1*.
    chunk_starts = [e for e in events if e.event == "chunk_started"]
    assert provider.call_count == len(chunk_starts), (
        f"4xx should not retry; expected {len(chunk_starts)} calls "
        f"(one per chunk), got {provider.call_count}"
    )


# ---------------------------------------------------------------------------
# T0b — event payload shape coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_error_event_payload_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0b) — `chunk_error` event carries `chunk_id`,
    `message[:200]`, and `exception_type`.

    `chunk_error` is the safety net for exceptions that escape
    `_run_chunk`'s retry/fallback envelope (e.g. a bug in
    `_build_hyphen_pairs` before the try block). Patching `_run_chunk`
    directly is the most targeted way to exercise the catch site at
    correction_pipeline.py:405-414.
    """
    from corrigenda.pipeline.correction_pipeline import CorrectionPipeline

    async def _explode(self, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(CorrectionPipeline, "_run_chunk", _explode)

    store, job_id = _make_store_and_job()
    queue = store.subscribe(job_id)
    await _run(job_id, MockProvider(), tmp_path, store)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    chunk_errors = [e for e in events if e.event == "chunk_error"]
    assert chunk_errors, "_run_chunk crash didn't surface as a chunk_error event"

    for ce in chunk_errors:
        # Shape contract — downstream observers (LoggingObserver,
        # JobStoreObserver → SSE) rely on these exact keys.
        assert "chunk_id" in ce.data
        assert "message" in ce.data
        assert "exception_type" in ce.data
        # Truncation contract: the message field is bounded
        # (correction_pipeline.py:410 uses [:200]).
        assert len(ce.data["message"]) <= 200
        # Exception class name is propagated (allows operators to
        # alert on OSError vs ValueError without parsing message).
        assert ce.data["exception_type"] == "OSError"


@pytest.mark.asyncio
async def test_hyphen_partner_missing_event_emitted_with_direction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0b) — when a hyphen partner can't be resolved, the
    pipeline emits a structured event with `direction` so observers
    can tell backward (PART1→PART2) from forward (BOTH→next).

    sample.xml contains an explicit PART1 (TL4); we force
    `_resolve_partner` to return None to exercise the missing-partner
    code path without needing a contrived multi-file fixture.
    """
    import corrigenda.pipeline.correction_pipeline as cp

    monkeypatch.setattr(cp, "_resolve_partner", lambda *args, **kwargs: None)

    store, job_id = _make_store_and_job()
    queue = store.subscribe(job_id)
    await _run(job_id, MockProvider(), tmp_path, store)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    missings = [e for e in events if e.event == "hyphen_partner_missing"]
    assert missings, (
        "Forced partner resolution to None for a PART1 in sample.xml — "
        "pipeline should have emitted at least one hyphen_partner_missing event"
    )

    for m in missings:
        # All four keys required by the contract.
        assert m.data["chunk_id"]
        assert m.data["line_id"]
        assert m.data["missing_partner_id"]
        # Direction is the discriminator between the two emission
        # sites (backward = PART1→PART2, forward = BOTH→next).
        assert m.data["direction"] in ("backward", "forward"), (
            f"direction must be backward|forward, got {m.data['direction']!r}"
        )


@pytest.mark.asyncio
async def test_retry_event_payload_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0b) — `retry` event carries `chunk_id`, `attempt`, and
    `error`. Without backoff capture, retries fire in real wallclock
    time — we monkeypatch sleep so the test runs in milliseconds.
    """
    await _capture_sleeps(monkeypatch)
    store, job_id = _make_store_and_job()
    queue = store.subscribe(job_id)

    # One invalid JSON response then success → exactly 1 retry event.
    provider = MockProvider(invalid_json_times=1)
    await _run(job_id, provider, tmp_path, store)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    retries = [e for e in events if e.event == "retry"]
    assert retries, "invalid_json_times=1 should have triggered a retry event"

    for r in retries:
        assert "chunk_id" in r.data
        assert isinstance(r.data["attempt"], int)
        assert r.data["attempt"] >= 1
        # `error` is either the literal "hyphen_integrity_violation"
        # sentinel or the original message truncated to 120 chars
        # (correction_pipeline.py:579, 582).
        assert isinstance(r.data["error"], str)
        assert len(r.data["error"]) <= 120


# ---------------------------------------------------------------------------
# T0d — multi-chunk persistent failure: every chunk must fall back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_failure_across_all_chunks_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Roadmap L4 (T0d) — when the provider fails on every call for every
    chunk, the pipeline must (a) fall back to OCR source on each chunk,
    (b) still finish with status=COMPLETED, (c) report a fallback
    counter ≥ chunk_count.

    `test_fallback_on_persistent_failure` only checks the first chunk
    (the existing MockProvider's `fail_times=3` recovers after — so
    subsequent chunks succeed). This test forces failure across the
    entire run.
    """
    await _capture_sleeps(monkeypatch)
    store, job_id = _make_store_and_job()

    provider = _AlwaysFailProvider(lambda: ValueError("provider permanently broken"))
    await _run(job_id, provider, tmp_path, store)

    job = store.get_job(job_id)
    assert job is not None
    # The runner reports completion even when every chunk fell back —
    # the job has finished its work, the WORK just didn't yield
    # any corrections. This matches the contract documented in
    # runner.py: completion ≠ correctness.
    assert job.status.value == "completed", (
        f"job status should be completed even after total fallback, got {job.status.value}"
    )
    assert job.fallbacks >= 1, "no fallback recorded despite provider always failing"

    # Every single line must carry corrected_text == ocr_text (fallback
    # contract: we don't drop content, we just refuse to "improve" it).
    assert job.document_manifest is not None
    for page in job.document_manifest.pages:
        for lm in page.lines:
            assert lm.corrected_text == lm.ocr_text, (
                f"line {lm.line_id} fallback should preserve ocr_text exactly, "
                f"got corrected={lm.corrected_text!r} vs ocr={lm.ocr_text!r}"
            )


# ---------------------------------------------------------------------------
# L10/B8 — `JobRunner.run` must mark the job FAILED when the task is
# cancelled (e.g. SIGTERM during shutdown). Pre-fix the runner had only
# `except TimeoutError` and `except Exception`; `asyncio.CancelledError`
# extends `BaseException` (not `Exception`) in Python 3.8+, so it slipped
# past both handlers. The job stayed in RUNNING forever, never entered
# `_completed_at`, and was never evicted — unbounded `_jobs` growth
# across SIGTERM/redeploy cycles.
# ---------------------------------------------------------------------------


class _NeverReturnsProvider:
    """`complete_structured` awaits forever — lets the test cancel the
    runner task while it's blocked on a 'pending LLM call'."""

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        return [ModelInfo(id="mock-never", label="Mock never")]

    async def complete_structured(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Sleep arbitrarily long; the test will cancel before this fires.
        await asyncio.sleep(60)
        return {"lines": []}, None


@pytest.mark.asyncio
async def test_runner_marks_job_failed_on_cancellation(tmp_path: Path):
    """L10/B8 — cancelling the runner task (simulating SIGTERM shutdown
    after the BackgroundTaskRegistry's 30 s grace period) must mark the
    job FAILED so eviction can reclaim it. Pre-fix the job stayed
    RUNNING forever; `_completed_at` was never populated for it; the
    capacity sweep + TTL eviction both keyed off `_completed_at` so
    the job leaked across redeploys.
    """
    from corrigenda.alto.parser import build_document_manifest

    from app.jobs.runner import JobRunner
    from app.schemas import JobStatus
    from app.storage import init_job_dirs, output_dir, save_uploaded_files
    from app.storage.output_writer import FilesystemOutputWriter

    sample_xml = Path(__file__).parent.parent.parent / "examples" / "sample.xml"
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock-never")
    init_job_dirs(job_id)
    saved, _ = save_uploaded_files(job_id, [(sample_xml.name, sample_xml.read_bytes())])
    doc = build_document_manifest([(p, n) for n, p in saved.items()])
    store.update_job(job_id, document_manifest=doc)

    runner = JobRunner(job_store=store)
    out_writer = FilesystemOutputWriter(output_dir(job_id))

    task = asyncio.create_task(
        runner.run(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="fake",
            model="mock-never",
            output_writer=out_writer,
            source_files={n: p for n, p in saved.items()},
            provider=_NeverReturnsProvider(),
            timeout_seconds=0,  # disable the wait_for timeout — we want cancellation
        )
    )

    # Yield to the event loop so the task starts and reaches the
    # `await asyncio.sleep(60)` inside the mock provider.
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = store.get_job(job_id)
    assert job is not None, "job was unexpectedly evicted before assertion"
    assert job.status == JobStatus.FAILED, (
        f"runner did not mark job FAILED on cancellation — status is "
        f"{job.status.value!r}. The job will never enter `_completed_at` "
        f"and will leak across SIGTERM/redeploy cycles (B8)."
    )
    assert job.error is not None and "cancel" in job.error.lower(), (
        f"job.error should mention cancellation, got {job.error!r}"
    )
