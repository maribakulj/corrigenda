"""ROADMAP V3 Phase 0 — PAGE XML travels the upload path.

The app has always advertised "ALTO/PAGE" (FastAPI description) while
`create_job` hard-wired the ALTO parser. On a valid PAGE file that
parser yields an EMPTY manifest (0 pages, 0 lines) without raising, so:
- a pure-PAGE upload was refused with a misleading "no text lines" 400;
- a mixed ALTO+PAGE upload silently dropped every PAGE line and
  completed on the ALTO subset alone.
`create_job` now builds its manifest through the format-sniffing
`corrigenda.formats.loader`, which also refuses mixed batches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import MockProvider  # reuse the provider mock

_EXAMPLES = Path(__file__).parent.parent.parent / "examples"
ALTO_SAMPLE = _EXAMPLES / "sample.xml"
PAGE_SAMPLE = (
    _EXAMPLES
    / "page"
    / "Descartes1637_Discours_btv1b86069594_corrected_0014_page_raw.xml"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with mocked providers and an isolated storage dir."""
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


def _form() -> dict:
    return {"provider": "openai", "api_key": "fake-key", "model": "mock-model"}


def _upload(path: Path):
    return ("files", (path.name, path.read_bytes(), "application/xml"))


def test_page_upload_creates_a_page_job(client: TestClient):
    """Failed before the loader switch: the ALTO parser read this PAGE
    file as 0 pages / 0 lines and the request died on a 400."""
    r = client.post("/api/jobs", data=_form(), files=[_upload(PAGE_SAMPLE)])
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    job = client.app.state.job_store.get_job(job_id)
    assert job is not None
    assert job.document_manifest is not None
    assert job.document_manifest.source_format == "page"
    assert job.document_manifest.total_lines > 0


def test_mixed_alto_page_upload_is_refused_not_silently_truncated(
    client: TestClient,
):
    """Failed before: the ALTO parser skipped the PAGE member without an
    error, so the job ran on the ALTO subset alone — silent data loss."""
    r = client.post(
        "/api/jobs",
        data=_form(),
        files=[_upload(ALTO_SAMPLE), _upload(PAGE_SAMPLE)],
    )
    assert r.status_code == 400
    assert "one document, one format" in r.json()["detail"]


def test_page_image_link_uses_the_declared_image_filename(tmp_path):
    """PAGE declares its scan on Page/@imageFilename (an attribute), not
    in ALTO's sourceImageInformation/fileName element — strategy 1 of
    the image linker must read both."""
    from app.storage import link_alto_to_images

    xml_copy = tmp_path / PAGE_SAMPLE.name
    xml_copy.write_bytes(PAGE_SAMPLE.read_bytes())
    # The fixture declares imageFilename="Descartes...0014.png"; the
    # uploaded image carries a DIFFERENT stem than the XML file, so the
    # stem fallback alone cannot make this link.
    image = tmp_path / "Descartes1637_Discours_btv1b86069594_corrected_0014.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    links = link_alto_to_images(
        pages=[("p1", xml_copy.name)],
        saved_alto={xml_copy.name: xml_copy},
        saved_images={image.stem.lower(): image},
    )
    assert links == {xml_copy.name: image.name}
