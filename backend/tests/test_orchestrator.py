"""Tests for jobs/orchestrator.py"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from app.alto.parser import build_document_manifest, parse_alto_file
from app.jobs.orchestrator import run_job
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider, SSEEvent

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
        self._fail_times = fail_times          # raise ValueError N times then succeed
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
            return {"bad_key": []}  # missing "lines"

        if self._fail_times > 0:
            self._fail_times -= 1
            raise ValueError("mock LLM error")

        # Return corrected_text identical to ocr_text (identity correction)
        lines_out = []
        for line_in in user_payload.get("lines", []):
            lines_out.append({
                "line_id": line_in["line_id"],
                "corrected_text": line_in["ocr_text"],
            })
        return {"lines": lines_out}


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
    """Run the orchestrator with the given store injected."""
    import app.jobs.orchestrator as orch_module
    # Temporarily replace the singleton
    orig_store = orch_module.job_store
    orch_module.job_store = store
    try:
        pages, _ = parse_alto_file(SAMPLE_XML, SAMPLE_XML.name)
        doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
        await run_job(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="fake-key",
            model="mock",
            output_dir=output_dir,
            source_files={SAMPLE_XML.name: SAMPLE_XML},
            provider=provider,
        )
    finally:
        orch_module.job_store = orig_store


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
    ns = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""

    def tag(local: str) -> str:
        return f"{{{ns}}}{local}" if ns else local

    tl_ids = {tl.get("ID") for tl in root.iter(tag("TextLine"))}
    # All original IDs must still be present
    orig_root = etree.parse(str(SAMPLE_XML)).getroot()
    orig_ns = orig_root.tag[1:orig_root.tag.index("}")] if orig_root.tag.startswith("{") else ""
    orig_ids = {tl.get("ID") for tl in orig_root.iter(f"{{{orig_ns}}}TextLine" if orig_ns else "TextLine")}
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

    ns_out = root_out.tag[1:root_out.tag.index("}")] if root_out.tag.startswith("{") else ""
    ns_orig = root_orig.tag[1:root_orig.tag.index("}")] if root_orig.tag.startswith("{") else ""

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
        assert out[line_id] == attrs, (
            f"{line_id} coords changed: {attrs} → {out[line_id]}"
        )


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
    ns = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""

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
    """MockProvider fails 3 times → OCR source kept, job still completed."""
    store, job_id = _make_store_and_job()
    # Fail more than max_attempts for the first chunk; succeed for the rest
    provider = MockProvider(fail_times=3)
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
    from app.alto.parser import build_document_manifest

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
            f'{body}'
            f'</PrintSpace></Page></Layout></alto>',
            encoding="utf-8",
        )

    doc = build_document_manifest([(file_a, "fileA.xml"), (file_b, "fileB.xml")])

    # Page IDs are disambiguated, so we have 2 distinct pages.
    assert len({p.page_id for p in doc.pages}) == 2

    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")

    import app.jobs.orchestrator as orch_module
    orig_store = orch_module.job_store
    orch_module.job_store = store
    try:
        await run_job(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="fake-key",
            model="mock",
            output_dir=tmp_path,
            source_files={"fileA.xml": file_a, "fileB.xml": file_b},
            provider=MockProvider(),
        )
    finally:
        orch_module.job_store = orig_store

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"

    # The cross-page partner (file B's TL1) must have been reconciled,
    # not silently skipped or paired with file A's own TL1.
    line_a_tl2 = next(
        lm for p in doc.pages if "fileA" in p.page_id
        for lm in p.lines if lm.line_id == "TL2"
    )
    line_b_tl1 = next(
        lm for p in doc.pages if "fileB" in p.page_id
        for lm in p.lines if lm.line_id == "TL1"
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
        return {"lines": []}


@pytest.mark.asyncio
async def test_job_timeout_marks_failure(tmp_path: Path):
    """When _JOB_TIMEOUT_SECONDS elapses, run_job catches TimeoutError,
    marks the job FAILED, emits a `failed` event, and records a clean
    error message (no traceback, no api_key leak)."""
    import app.jobs.orchestrator as orch_module

    store, job_id = _make_store_and_job()
    orig_timeout = orch_module._JOB_TIMEOUT_SECONDS
    orig_store = orch_module.job_store
    orch_module._JOB_TIMEOUT_SECONDS = 1  # 1-second budget — provider sleeps 5s
    orch_module.job_store = store
    try:
        pages, _ = parse_alto_file(SAMPLE_XML, SAMPLE_XML.name)
        doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
        await run_job(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="sk-secret-token-12345",
            model="mock",
            output_dir=tmp_path,
            source_files={SAMPLE_XML.name: SAMPLE_XML},
            provider=_SlowProvider(),
        )
    finally:
        orch_module._JOB_TIMEOUT_SECONDS = orig_timeout
        orch_module.job_store = orig_store

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
    import app.jobs.orchestrator as orch_module
    from app.jobs.correction_pipeline import CorrectionPipeline

    store, job_id = _make_store_and_job()
    orig_store = orch_module.job_store
    orch_module.job_store = store

    async def _boom(self, **kwargs):
        raise RuntimeError("simulated failure carrying sk-secret-token-12345")

    monkeypatch.setattr(CorrectionPipeline, "run", _boom)
    try:
        doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
        await run_job(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="sk-secret-token-12345",
            model="mock",
            output_dir=tmp_path,
            source_files={SAMPLE_XML.name: SAMPLE_XML},
            provider=MockProvider(),
        )
    finally:
        orch_module.job_store = orig_store

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
    import app.jobs.orchestrator as orch_module

    mock = MockProvider()
    monkeypatch.setattr(prov_module, "get_provider", lambda p: mock)

    store, job_id = _make_store_and_job()
    orig_store = orch_module.job_store
    orch_module.job_store = store
    try:
        doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
        await run_job(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="fake-key",
            model="mock",
            output_dir=tmp_path,
            source_files={SAMPLE_XML.name: SAMPLE_XML},
            # Note: provider arg omitted → registry lookup path
        )
    finally:
        orch_module.job_store = orig_store

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"
