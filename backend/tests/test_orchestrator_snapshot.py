"""Snapshot tests for orchestrator output.

These tests pin the byte-level output of the correction pipeline on
two reference corpora (a tiny one and a large one). Their purpose is
to detect any unintended behavioural change during the upcoming
refactoring (see MIGRATION.md §8.1).

The identity-correction MockProvider returns each line's OCR text
unchanged, so the output should be a deterministic transformation of
the input that depends only on the parser/rewriter/reconciler logic.

If a snapshot must change (e.g. an intentional rewriter improvement),
update the constants below in the same commit and justify it in the
commit message.
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.alto.parser import build_document_manifest
from app.jobs.orchestrator import run_job
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
SAMPLE_XML = EXAMPLES_DIR / "sample.xml"
X0000002_XML = EXAMPLES_DIR / "X0000002.xml"


# ---------------------------------------------------------------------------
# Identity MockProvider — deterministic, returns OCR text unchanged
# ---------------------------------------------------------------------------

class _IdentityProvider:
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
        return {
            "lines": [
                {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
                for line in user_payload.get("lines", [])
            ]
        }


def _run_and_capture(xml_path: Path) -> dict[str, Any]:
    """Run the orchestrator and return a snapshot of observable outputs."""
    import app.jobs.orchestrator as orch_module

    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    orig_store = orch_module.job_store
    orch_module.job_store = store
    try:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            doc = build_document_manifest([(xml_path, xml_path.name)])
            asyncio.run(run_job(
                job_id=job_id,
                document_manifest=doc,
                provider_name="openai",
                api_key="fake-key",
                model="mock",
                output_dir=out_dir,
                source_files={xml_path.name: xml_path},
                provider=_IdentityProvider(),
            ))
            job = store.get_job(job_id)
            assert job is not None

            out_xml = next(out_dir.glob("*_corrected.xml"))
            xml_bytes = out_xml.read_bytes()

            return {
                "xml_sha256": hashlib.sha256(xml_bytes).hexdigest(),
                "xml_size": len(xml_bytes),
                "status": job.status.value,
                "total_lines": job.total_lines,
                "lines_modified": job.lines_modified,
                "chunks_total": job.chunks_total,
                "retries": job.retries,
                "fallbacks": job.fallbacks,
                "trace_count": len(job.line_traces),
            }
    finally:
        orch_module.job_store = orig_store


# ---------------------------------------------------------------------------
# Snapshots — pin baseline values captured before refactoring
# ---------------------------------------------------------------------------

SAMPLE_SNAPSHOT = {
    "xml_sha256": "10eda74a8afbc2eb3a1c3cf5dd488091f05388e887d17a4f86343e5a54855ec7",
    "xml_size": 10058,
    "status": "completed",
    "total_lines": 10,
    "lines_modified": 0,
    "chunks_total": 2,
    "retries": 0,
    "fallbacks": 0,
    "trace_count": 10,
}

X0000002_SNAPSHOT = {
    "xml_sha256": "18387a3d4dfdd2a117a0bf4593d9533da3f5aeef35edd6c8a5b5e3d875c759b6",
    "xml_size": 654955,
    "status": "completed",
    "total_lines": 566,
    "lines_modified": 0,
    "chunks_total": 52,
    "retries": 0,
    "fallbacks": 0,
    "trace_count": 566,
}


def test_snapshot_sample_xml():
    """Tiny corpus — fast regression detector."""
    assert _run_and_capture(SAMPLE_XML) == SAMPLE_SNAPSHOT


@pytest.mark.skipif(not X0000002_XML.exists(), reason="X0000002.xml not in examples/")
def test_snapshot_x0000002_xml():
    """Large corpus — detects subtler rewriter changes that the tiny
    sample might miss (cross-block hyphenation, many chunks, etc.)."""
    assert _run_and_capture(X0000002_XML) == X0000002_SNAPSHOT
