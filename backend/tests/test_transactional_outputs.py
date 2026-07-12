"""P0-4 — transactional outputs: nothing partial is ever downloadable.

Historically the writer wrote each corrected XML directly under its
final name as the run progressed, and /download only checked that SOME
output file existed — so a job that failed on file 2 of 3 stayed FAILED
while /download served file 1 (with no trace.json and no indication the
set was incomplete).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import JobStatus, Provider
from app.storage.output_writer import STAGING_DIRNAME, FilesystemOutputWriter

SAMPLE_XML = Path(__file__).resolve().parent.parent.parent / "examples" / "sample.xml"


# ---------------------------------------------------------------------------
# Writer unit behaviour
# ---------------------------------------------------------------------------


def test_writes_are_staged_not_final(tmp_path: Path):
    w = FilesystemOutputWriter(tmp_path)
    w.write_corrected(source_stem="a", xml_bytes=b"<xml/>")
    w.write_trace(traces_payload="{}")
    # Nothing visible in the final directory before commit.
    visible = [p for p in tmp_path.iterdir() if p.name != STAGING_DIRNAME]
    assert visible == []
    assert (tmp_path / STAGING_DIRNAME / "a_corrected.xml").exists()


def test_commit_promotes_everything_atomically(tmp_path: Path):
    w = FilesystemOutputWriter(tmp_path)
    w.write_corrected(source_stem="a", xml_bytes=b"<a/>")
    w.write_corrected(source_stem="b", xml_bytes=b"<b/>")
    w.write_trace(traces_payload="{}")
    w.commit()
    assert (tmp_path / "a_corrected.xml").read_bytes() == b"<a/>"
    assert (tmp_path / "b_corrected.xml").read_bytes() == b"<b/>"
    assert (tmp_path / "trace.json").exists()
    assert not (tmp_path / STAGING_DIRNAME).exists()


def test_discard_leaves_final_directory_untouched(tmp_path: Path):
    w = FilesystemOutputWriter(tmp_path)
    w.write_corrected(source_stem="a", xml_bytes=b"<a/>")
    w.discard()
    assert list(tmp_path.iterdir()) == []


def test_commit_with_nothing_staged_is_a_noop(tmp_path: Path):
    FilesystemOutputWriter(tmp_path).commit()  # dry-run path — no crash
    assert not (tmp_path / STAGING_DIRNAME).exists()


def test_commit_rolls_back_partial_promotion_on_error(tmp_path: Path, monkeypatch):
    """Audit P3 — if a later file fails to promote (ENOSPC/EIO), the files
    already moved must be rolled back so the output directory is never left
    with a partial set (the documented all-or-nothing contract)."""
    import os as _os

    w = FilesystemOutputWriter(tmp_path)
    w.write_corrected(source_stem="a", xml_bytes=b"<a/>")
    w.write_corrected(source_stem="b", xml_bytes=b"<b/>")
    w.write_trace(traces_payload="{}")

    real_replace = _os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # let the first promote, blow up on the second
            raise OSError("simulated ENOSPC")
        return real_replace(src, dst)

    monkeypatch.setattr("app.storage.output_writer.os.replace", flaky_replace)

    with pytest.raises(OSError):
        w.commit()

    # No partial set survives in the output directory: every promoted file
    # was rolled back. (Anything left is inside the hidden staging dir.)
    visible = [p for p in tmp_path.iterdir() if p.name != STAGING_DIRNAME]
    assert visible == []


def test_get_output_files_never_sees_staging(tmp_path: Path, monkeypatch):
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path)
    job_id = "j1"
    out = storage_module.output_dir(job_id)
    w = FilesystemOutputWriter(out)
    w.write_corrected(source_stem="a", xml_bytes=b"<a/>")
    assert storage_module.get_output_files(job_id) == []
    w.commit()
    assert [p.name for p in storage_module.get_output_files(job_id)] == ["a_corrected.xml"]


# ---------------------------------------------------------------------------
# Runner integration: failure mid-run leaves nothing downloadable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_job_leaves_no_output(tmp_path: Path):
    """A run whose FIRST file rewrote fine but whose overall pipeline
    then fails must expose nothing: staged files are discarded and
    /download's state guard refuses anyway."""
    from corrigenda.core.protocols import ProviderPermanentError
    from corrigenda.formats.alto.parser import build_document_manifest

    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore

    class _RejectingProvider:
        async def list_models(self, api_key):  # pragma: no cover
            return []

        async def complete_structured(self, **_):
            raise ProviderPermanentError("rejected", status_code=401)

    store = JobStore()
    job_id = store.create_job(Provider("openai"), "mock")
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    out_dir = tmp_path / "out"
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="k",
        model="mock",
        output_writer=FilesystemOutputWriter(out_dir),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=_RejectingProvider(),
    )
    job = store.get_job(job_id)
    assert job is not None and job.status == JobStatus.FAILED
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# /download state guard (end to end)
# ---------------------------------------------------------------------------


def test_download_refuses_failed_job_even_with_files_on_disk(tmp_path, monkeypatch):
    """THE audit scenario: files exist on disk but the job is FAILED —
    /download must refuse instead of serving a partial set."""
    from fastapi.testclient import TestClient

    from app import storage as storage_module
    from app.main import create_app

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        store = client.app.state.job_store
        job_id = store.create_job(Provider("openai"), "mock")
        # Simulate the historical partial write: a file directly in the
        # output dir of a job that then FAILED.
        out = storage_module.output_dir(job_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / "partial_corrected.xml").write_bytes(b"<xml/>")
        store.update_job(job_id, status=JobStatus.FAILED, error="boom")

        r = client.get(f"/api/jobs/{job_id}/download")
        assert r.status_code == 409
        assert "not in a downloadable state" in r.json()["detail"]
