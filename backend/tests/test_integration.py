"""Integration tests: full correction pipeline end-to-end."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from alto_core.alto.parser import build_document_manifest, parse_alto_file
from fastapi.testclient import TestClient
from lxml import etree

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider
from app.storage import (
    get_output_files,
    init_job_dirs,
    link_alto_to_images,
    output_dir,
    save_uploaded_files,
)
from app.storage.output_writer import FilesystemOutputWriter

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"
NS = "http://www.loc.gov/standards/alto/ns-v3#"


# ---------------------------------------------------------------------------
# MockProvider
# ---------------------------------------------------------------------------


class MockProvider:
    """Identity correction: returns each line's OCR text unchanged."""

    def __init__(self, invalid_json_times: int = 0) -> None:
        self._bad = invalid_json_times

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        return [ModelInfo(id="mock", label="Mock")]

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        if self._bad > 0:
            self._bad -= 1
            return {"bad_key": []}, None  # missing "lines" → validation error

        lines_out = [
            {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
            for line in user_payload.get("lines", [])
        ]
        return {"lines": lines_out}, None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


def _run_job_directly(
    source_bytes: dict[str, bytes],
    mock: MockProvider | None = None,
    store: JobStore | None = None,
) -> tuple[str, list[Path], JobStore]:
    """Create a job and run it synchronously.

    Returns ``(job_id, output_paths, store)``. If `store` is not supplied,
    a fresh one is created so tests don't accumulate state across runs.
    """
    if mock is None:
        mock = MockProvider()
    if store is None:
        store = JobStore()

    job_id = store.create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    saved, _ = save_uploaded_files(job_id, list(source_bytes.items()))
    doc = build_document_manifest([(p, n) for n, p in saved.items()])
    store.update_job(job_id, document_manifest=doc)

    out_dir = output_dir(job_id)
    _run(
        JobRunner(job_store=store).run(
            job_id=job_id,
            document_manifest=doc,
            provider_name="openai",
            api_key="fake-key",
            model="mock",
            output_writer=FilesystemOutputWriter(out_dir),
            source_files={n: p for n, p in saved.items()},
            provider=mock,
        )
    )

    return job_id, get_output_files(job_id), store


def _make_client(
    mock: MockProvider | None = None,
    store: JobStore | None = None,
) -> TestClient:
    """Return a TestClient with the given (or default) MockProvider injected.

    If `store` is provided, it replaces the app's default JobStore so
    callers can share state between direct `run_job` calls and HTTP
    requests against the client.
    """
    from app import providers as prov_module
    from app.main import create_app

    if mock is None:
        mock = MockProvider()

    orig = prov_module._REGISTRY.copy()
    for k in list(prov_module._REGISTRY):
        prov_module._REGISTRY[k] = mock

    app = create_app()
    if store is not None:
        app.state.job_store = store
    client = TestClient(app, raise_server_exceptions=False)
    # stash for cleanup
    client._orig = orig  # type: ignore[attr-defined]
    client._prov = prov_module  # type: ignore[attr-defined]
    return client


def _restore(client: TestClient) -> None:
    client._prov._REGISTRY.update(client._orig)  # type: ignore[attr-defined]


def _upload_sample(
    client: TestClient,
    files: list[tuple[str, bytes, str]] | None = None,
):
    """POST /api/jobs with sample.xml (or custom files list)."""
    if files is None:
        files = [(SAMPLE_XML.name, SAMPLE_XML.read_bytes(), "application/xml")]
    multipart = [("files", (name, content, ctype)) for name, content, ctype in files]
    return client.post(
        "/api/jobs",
        data={"provider": "openai", "api_key": "x", "model": "mock"},
        files=multipart,
    )


def _poll_completed(client: TestClient, job_id: str, tries: int = 80) -> str:
    """
    Poll GET /api/jobs/{id} until terminal state.

    The background asyncio.create_task runs in TestClient's portal thread;
    time.sleep releases the GIL and lets that thread advance the event loop.
    """
    for _ in range(tries):
        time.sleep(0.1)
        status = client.get(f"/api/jobs/{job_id}").json()["status"]
        if status in ("completed", "failed"):
            return status
    return "timeout"


# ---------------------------------------------------------------------------
# test_upload_single_xml
# ---------------------------------------------------------------------------


def test_upload_single_xml():
    """POST /api/jobs with sample.xml → 200 + job_id, job ends completed."""
    client = _make_client()
    try:
        resp = _upload_sample(client)
        assert resp.status_code == 200
        body = resp.json()
        assert "job_id" in body

        status = _poll_completed(client, body["job_id"])
        assert status == "completed"
    finally:
        _restore(client)


# ---------------------------------------------------------------------------
# test_upload_zip
# ---------------------------------------------------------------------------


def test_upload_zip():
    """ZIP archive containing 2 XML files → job completed, 2 output files."""
    buf = io.BytesIO()
    xml_data = SAMPLE_XML.read_bytes()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("page1.xml", xml_data)
        zf.writestr("page2.xml", xml_data)

    client = _make_client()
    try:
        resp = client.post(
            "/api/jobs",
            data={"provider": "openai", "api_key": "x", "model": "mock"},
            files=[("files", ("archive.zip", buf.getvalue(), "application/zip"))],
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        status = _poll_completed(client, job_id)
        assert status == "completed"

        assert len(get_output_files(job_id)) == 2
    finally:
        _restore(client)


# ---------------------------------------------------------------------------
# test_upload_invalid_extension
# ---------------------------------------------------------------------------


def test_upload_invalid_extension():
    """Uploading a .pdf file returns HTTP 400."""
    client = _make_client()
    try:
        resp = client.post(
            "/api/jobs",
            data={"provider": "openai", "api_key": "x", "model": "mock"},
            files=[("files", ("document.pdf", b"%PDF-1.4", "application/pdf"))],
        )
        assert resp.status_code == 400
    finally:
        _restore(client)


# ---------------------------------------------------------------------------
# test_sse_events_order
# ---------------------------------------------------------------------------


def test_sse_events_order():
    """
    Events emitted during a complete job appear in the expected order:
    started → document_parsed → page_started → chunk_planned
    → chunk_started → chunk_completed → page_completed → completed
    """
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    saved, _ = save_uploaded_files(job_id, [(SAMPLE_XML.name, SAMPLE_XML.read_bytes())])
    doc = build_document_manifest([(p, n) for n, p in saved.items()])
    store.update_job(job_id, document_manifest=doc)

    # Subscribe BEFORE running so every emitted event lands in the queue
    queue = store.subscribe(job_id)

    try:
        _run(
            JobRunner(job_store=store).run(
                job_id=job_id,
                document_manifest=doc,
                provider_name="openai",
                api_key="fake-key",
                model="mock",
                output_writer=FilesystemOutputWriter(output_dir(job_id)),
                source_files={n: p for n, p in saved.items()},
                provider=MockProvider(),
            )
        )
    finally:
        # Drain queue, then unsubscribe
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        store.unsubscribe(job_id, queue)

    names = [e.event for e in events]

    assert names[-1] == "completed", f"Last event should be 'completed', got {names[-1]!r}"
    assert names[0] == "started", f"First event should be 'started', got {names[0]!r}"

    required_order = [
        "started",
        "document_parsed",
        "page_started",
        "chunk_planned",
        "chunk_started",
        "chunk_completed",
        "page_completed",
        "completed",
    ]

    positions: dict[str, int] = {}
    for ev in required_order:
        idx = next((i for i, n in enumerate(names) if n == ev), None)
        assert idx is not None, f"Expected event {ev!r} not found. Got: {names}"
        positions[ev] = idx

    for a, b in zip(required_order, required_order[1:]):
        assert positions[a] < positions[b], (
            f"Event {a!r} (pos {positions[a]}) must precede {b!r} (pos {positions[b]})"
        )


# ---------------------------------------------------------------------------
# test_download_single_xml
# ---------------------------------------------------------------------------


def test_download_single_xml():
    """Completed single-file job → GET download returns application/xml parseable by lxml."""
    job_id, out_files, store = _run_job_directly({SAMPLE_XML.name: SAMPLE_XML.read_bytes()})
    assert len(out_files) == 1

    client = _make_client(store=store)
    try:
        resp = client.get(f"/api/jobs/{job_id}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/xml")
        # Must be valid XML
        etree.fromstring(resp.content)
    finally:
        _restore(client)


# ---------------------------------------------------------------------------
# test_download_multi_zip
# ---------------------------------------------------------------------------


def test_download_multi_zip():
    """Completed 2-file job → GET download returns application/zip with 2 entries."""
    xml_data = SAMPLE_XML.read_bytes()
    job_id, out_files, store = _run_job_directly({"page1.xml": xml_data, "page2.xml": xml_data})
    assert len(out_files) == 2

    client = _make_client(store=store)
    try:
        resp = client.get(f"/api/jobs/{job_id}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/zip")

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert len(zf.namelist()) == 2
    finally:
        _restore(client)


# ---------------------------------------------------------------------------
# test_fallback_on_invalid_json
# ---------------------------------------------------------------------------


def test_fallback_on_invalid_json():
    """
    Provider returns invalid JSON persistently → orchestrator falls back
    to OCR source text. Job still completes successfully.
    asyncio.sleep is mocked so the retry back-off doesn't slow the test.

    F1 — a transient burst of invalid JSON now recovers via granularity
    downgrade, so the failure must persist past the per-chunk budget to
    force the terminal fallback.
    """
    mock = MockProvider(invalid_json_times=99)

    # Patch alto-core directly: the backend shim no longer re-exports
    # the `asyncio` module attribute (Stage 3 audit remediation).
    with patch(
        "alto_core.pipeline.correction_pipeline.asyncio.sleep", new=AsyncMock(return_value=None)
    ):
        job_id, out_files, store = _run_job_directly(
            {SAMPLE_XML.name: SAMPLE_XML.read_bytes()},
            mock=mock,
        )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "completed"
    assert job.fallbacks > 0, "Expected at least one fallback to OCR source"

    # Output must still be valid ALTO XML
    assert len(out_files) == 1
    etree.parse(str(out_files[0]))


# ---------------------------------------------------------------------------
# test_output_preserves_textline_ids
# ---------------------------------------------------------------------------


def test_output_preserves_textline_ids():
    """Output XML has the same TextLine IDs in the same order as the source."""
    src_pages, _ = parse_alto_file(SAMPLE_XML, SAMPLE_XML.name)
    src_ids = [lm.line_id for p in src_pages for lm in p.lines]

    _, out_files, _ = _run_job_directly({SAMPLE_XML.name: SAMPLE_XML.read_bytes()})
    assert len(out_files) == 1

    out_pages, _ = parse_alto_file(out_files[0], out_files[0].name)
    out_ids = [lm.line_id for p in out_pages for lm in p.lines]

    assert src_ids == out_ids, f"TextLine IDs differ.\nSource: {src_ids}\nOutput: {out_ids}"


# ---------------------------------------------------------------------------
# test_output_preserves_textline_coords
# ---------------------------------------------------------------------------


def test_output_preserves_textline_coords():
    """Each output TextLine has identical HPOS/VPOS/WIDTH/HEIGHT to the source."""
    src_pages, _ = parse_alto_file(SAMPLE_XML, SAMPLE_XML.name)
    src_coords = {
        lm.line_id: (lm.coords.hpos, lm.coords.vpos, lm.coords.width, lm.coords.height)
        for p in src_pages
        for lm in p.lines
    }

    _, out_files, _ = _run_job_directly({SAMPLE_XML.name: SAMPLE_XML.read_bytes()})
    out_pages, _ = parse_alto_file(out_files[0], out_files[0].name)
    out_coords = {
        lm.line_id: (lm.coords.hpos, lm.coords.vpos, lm.coords.width, lm.coords.height)
        for p in out_pages
        for lm in p.lines
    }

    for line_id, sc in src_coords.items():
        assert out_coords.get(line_id) == sc, (
            f"Coords mismatch on {line_id}: source={sc}, output={out_coords.get(line_id)}"
        )


# ---------------------------------------------------------------------------
# test_hyphen_pairs_in_output
# ---------------------------------------------------------------------------


def test_hyphen_pairs_in_output():
    """
    Explicit hyphen pair TL4/TL5 (SUBS_TYPE in source) is preserved in output:
    - TL4 has a <HYP> element
    - TL4 first String has SUBS_TYPE="HypPart1"
    - TL5 first String has SUBS_TYPE="HypPart2"
    - Both carry matching non-empty SUBS_CONTENT
    """
    _, out_files, _ = _run_job_directly({SAMPLE_XML.name: SAMPLE_XML.read_bytes()})
    assert len(out_files) == 1

    tree = etree.parse(str(out_files[0]))
    nsp = {"a": NS}

    # TL4 must contain <HYP>
    hyp_els = tree.xpath("//a:TextLine[@ID='TL4']/a:HYP", namespaces=nsp)
    assert hyp_els, "TL4 must contain a <HYP> element"

    # TL4 String with SUBS_TYPE="HypPart1"
    part1 = tree.xpath(
        "//a:TextLine[@ID='TL4']/a:String[@SUBS_TYPE='HypPart1']",
        namespaces=nsp,
    )
    assert part1, "TL4 must have a String with SUBS_TYPE='HypPart1'"

    # TL5 String with SUBS_TYPE="HypPart2"
    part2 = tree.xpath(
        "//a:TextLine[@ID='TL5']/a:String[@SUBS_TYPE='HypPart2']",
        namespaces=nsp,
    )
    assert part2, "TL5 must have a String with SUBS_TYPE='HypPart2'"

    # SUBS_CONTENT must be present and match on both sides
    sc1 = part1[0].get("SUBS_CONTENT")
    sc2 = part2[0].get("SUBS_CONTENT")
    assert sc1, "TL4 SUBS_CONTENT must not be empty"
    assert sc2, "TL5 SUBS_CONTENT must not be empty"
    assert sc1 == sc2, f"SUBS_CONTENT mismatch: {sc1!r} vs {sc2!r}"


# ---------------------------------------------------------------------------
# test_heuristic_hyphen_no_subs_content
# ---------------------------------------------------------------------------


def test_heuristic_hyphen_no_subs_content():
    """
    Heuristic hyphen pair TL6/TL7 (no SUBS_TYPE in source, detected by trailing dash)
    must not have any invented SUBS_CONTENT in the output.
    """
    _, out_files, _ = _run_job_directly({SAMPLE_XML.name: SAMPLE_XML.read_bytes()})
    assert len(out_files) == 1

    tree = etree.parse(str(out_files[0]))
    nsp = {"a": NS}

    for line_id in ("TL6", "TL7"):
        strings = tree.xpath(
            f"//a:TextLine[@ID='{line_id}']/a:String",
            namespaces=nsp,
        )
        assert strings, f"{line_id} not found in output"
        for s in strings:
            assert s.get("SUBS_CONTENT") is None, (
                f"{line_id} must not have SUBS_CONTENT (heuristic pair), "
                f"got {s.get('SUBS_CONTENT')!r}"
            )


# ---------------------------------------------------------------------------
# test_zip_with_images
# ---------------------------------------------------------------------------


def test_zip_with_images():
    """ZIP with XML + PNG → image saved, job completes, image served via API."""
    # Minimal valid PNG magic (backend stores and serves without parsing content)
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    xml_data = SAMPLE_XML.read_bytes()
    # The image stem must match the XML stem for auto-linking
    xml_name = SAMPLE_XML.stem + ".xml"
    img_name = SAMPLE_XML.stem + ".png"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(xml_name, xml_data)
        zf.writestr(img_name, fake_png)

    client = _make_client()
    try:
        resp = client.post(
            "/api/jobs",
            data={"provider": "openai", "api_key": "x", "model": "mock"},
            files=[("files", ("archive.zip", buf.getvalue(), "application/zip"))],
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        status = _poll_completed(client, job_id)
        assert status == "completed"

        # Layout should reference the image
        layout = client.get(f"/api/jobs/{job_id}/layout").json()
        image_url = layout["pages"][0].get("image_url")
        assert image_url is not None, "image_url should be set when image matches ALTO stem"

        # Image endpoint should return the PNG bytes
        img_resp = client.get(image_url)
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"] == "image/png"
        assert img_resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        _restore(client)


# ---------------------------------------------------------------------------
# test_link_alto_to_images_by_filename
# ---------------------------------------------------------------------------


def test_link_alto_to_images_by_filename():
    """link_alto_to_images matches by lowercase stem of the ALTO source file."""
    job_id = JobStore().create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    xml_data = SAMPLE_XML.read_bytes()
    # Use a predictable name so stem is known
    files = [("mypage.xml", xml_data)]
    saved_alto, _ = save_uploaded_files(job_id, files)

    # Simulate an image whose stem matches the XML stem
    from app.storage import images_dir

    imgs = images_dir(job_id)
    imgs.mkdir(parents=True, exist_ok=True)
    img_path = imgs / "mypage.jpg"
    img_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)  # fake JPEG

    saved_images = {"mypage": img_path}

    # Build a minimal pages list
    doc = build_document_manifest([(p, n) for n, p in saved_alto.items()])
    pages_info = [(p.page_id, p.source_file) for p in doc.pages]

    result = link_alto_to_images(pages_info, saved_alto, saved_images)

    # Result is keyed by source_file (not page_id) to handle ALTO files that
    # all declare the same Page/@ID (e.g. ID="Page1").
    unique_sources = {sf for _, sf in pages_info}
    assert len(result) == len(unique_sources), (
        f"Expected one entry per unique source file ({len(unique_sources)}), got {result}"
    )
    assert result.get("mypage.xml") == "mypage.jpg"


# ---------------------------------------------------------------------------
# test_link_alto_to_images_no_match
# ---------------------------------------------------------------------------


def test_link_alto_to_images_no_match():
    """link_alto_to_images returns empty dict when no stem matches."""
    job_id = JobStore().create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    xml_data = SAMPLE_XML.read_bytes()
    saved_alto, _ = save_uploaded_files(job_id, [("doc_a.xml", xml_data)])

    # Image has a different stem → no match
    saved_images = {"doc_b": Path("/tmp/nonexistent/doc_b.png")}

    doc = build_document_manifest([(p, n) for n, p in saved_alto.items()])
    pages_info = [(p.page_id, p.source_file) for p in doc.pages]

    result = link_alto_to_images(pages_info, saved_alto, saved_images)

    assert result == {}, f"Expected no matches, got {result}"


# ---------------------------------------------------------------------------
# macOS ZIP artefact tests
# ---------------------------------------------------------------------------


def _make_macos_zip(xml_bytes: bytes, xml_name: str) -> bytes:
    """
    Build a ZIP that mimics what macOS Finder produces:
    - the real XML file
    - an AppleDouble metadata file  ._<name>  at the root
    - the same AppleDouble file inside __MACOSX/
    - a dummy image and its AppleDouble counterpart
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(xml_name, xml_bytes)  # real file
        zf.writestr(f"._{xml_name}", b"\x00\x05\x16\x07fakeAD")  # AppleDouble at root
        zf.writestr(f"__MACOSX/._{xml_name}", b"\x00\x05\x16\x07fakeAD")  # inside __MACOSX
        zf.writestr("scan.jpg", b"\xff\xd8\xff" + b"\x00" * 16)  # real image
        zf.writestr("._scan.jpg", b"\x00\x05\x16\x07fakeAD")  # AppleDouble image
        zf.writestr("__MACOSX/._scan.jpg", b"\x00\x05\x16\x07")  # inside __MACOSX
    buf.seek(0)
    return buf.read()


def test_macos_zip_skips_appledouble_xml():
    """._<name>.xml AppleDouble files must never reach the XML parser."""
    job_id = JobStore().create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    xml_bytes = SAMPLE_XML.read_bytes()
    zip_bytes = _make_macos_zip(xml_bytes, "0000.xml")

    saved, images = save_uploaded_files(job_id, [("archive.zip", zip_bytes)])

    # Only the real XML must be saved — not ._0000.xml
    assert list(saved.keys()) == ["0000.xml"], f"Expected only '0000.xml', got {list(saved.keys())}"
    # Only the real image must be saved — not ._scan.jpg
    assert list(images.keys()) == ["scan"], f"Expected only 'scan', got {list(images.keys())}"


def test_macos_zip_parses_without_error():
    """A macOS ZIP must parse cleanly (no 'Document is empty' error)."""
    job_id = JobStore().create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    xml_bytes = SAMPLE_XML.read_bytes()
    zip_bytes = _make_macos_zip(xml_bytes, "page.xml")

    saved, _ = save_uploaded_files(job_id, [("archive.zip", zip_bytes)])

    # Must not raise
    doc = build_document_manifest([(p, n) for n, p in saved.items()])
    assert doc.total_lines > 0


# ---------------------------------------------------------------------------
# ZIP safety-limit tests
# ---------------------------------------------------------------------------


def test_zip_rejected_when_too_many_members():
    """ZIP with more members than _MAX_ZIP_MEMBERS is rejected before extraction."""
    from app import storage as storage_mod

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # A handful of tiny members — we patch the limit lower than this count
        for i in range(20):
            zf.writestr(f"f{i:03d}.xml", b"<x/>")

    job_id = JobStore().create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    with patch.object(storage_mod, "_MAX_ZIP_MEMBERS", 10):
        with pytest.raises(ValueError, match="too many members"):
            save_uploaded_files(job_id, [("archive.zip", buf.getvalue())])


def test_zip_rejected_when_declared_size_exceeds_limit():
    """ZIP whose declared aggregate uncompressed size > limit is rejected."""
    from app import storage as storage_mod

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 10 KB of XML — patch the limit below this to trigger rejection
        zf.writestr("big.xml", b"<x/>" * 2500)

    job_id = JobStore().create_job(Provider.OPENAI, "mock")
    init_job_dirs(job_id)

    with patch.object(storage_mod, "_MAX_ZIP_EXTRACTED_BYTES", 1024):
        with pytest.raises(ValueError, match="declared uncompressed size"):
            save_uploaded_files(job_id, [("archive.zip", buf.getvalue())])


def test_safe_zip_read_aborts_on_oversize_member():
    """_safe_zip_read raises if a member's actual extracted size exceeds the
    caller-supplied budget — catches lying central-directory entries."""
    from app.storage import _safe_zip_read

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.xml", b"x" * 200_000)

    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        member = zf.infolist()[0]
        with pytest.raises(ValueError, match="extraction safety limit"):
            _safe_zip_read(zf, member, remaining_bytes=1024)


def test_safe_zip_read_passes_under_budget():
    """_safe_zip_read returns the full member when within budget."""
    from app.storage import _safe_zip_read

    buf = io.BytesIO()
    payload = b"y" * 5_000
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.xml", payload)

    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        member = zf.infolist()[0]
        data = _safe_zip_read(zf, member, remaining_bytes=10_000)
        assert data == payload
