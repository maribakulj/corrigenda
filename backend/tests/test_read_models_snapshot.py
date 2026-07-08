"""Characterization snapshot of the /diff and /layout read-model
projections (audit Phase 1 — filet for audit Problems 4 and 9).

Problem 4: ``api/jobs.py`` is a 498-line router that inlines the /diff and
/layout manifest→JSON projections. Phase 5 will lift them into pure
functions in a read-model module. This test pins their *exact output
shape* on a small deterministic corpus (``examples/sample.xml``, 2 pages /
10 lines / 3 blocks) by calling the endpoint functions directly with a
hand-built ``JobManifest`` — no HTTP, no job store. After the Phase-5
extraction, the pure functions must reproduce this shape byte-for-byte.

Problem 9: the projections read ``job.document_manifest.pages`` only. This
test builds the ``JobManifest`` with an EMPTY ``line_traces`` map and still
gets full output — proof that ``line_traces`` is dead for these endpoints
and safe to delete in Phase 2.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from corrigenda.formats.alto.parser import build_document_manifest

from app.api.jobs import get_job_diff, get_job_layout
from app.schemas.job import JobManifest, JobStatus, Provider

SAMPLE = Path(__file__).resolve().parent.parent.parent / "examples" / "sample.xml"

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample.xml not found")


def _job() -> JobManifest:
    doc = build_document_manifest([(SAMPLE, "sample.xml")])
    # line_traces deliberately left empty (default) — Problem 9: the
    # projections must not depend on it.
    return JobManifest(
        job_id="snap",
        provider=Provider.OPENAI,
        model="m",
        status=JobStatus.COMPLETED,
        document_manifest=doc,
    )


_LINE_KEYS = {
    "line_id",
    "ocr_text",
    "corrected_text",
    "modified",
    "hyphen_role",
    "hyphen_subs_content",
}
_LAYOUT_LINE_KEYS = {
    "line_id",
    "hpos",
    "vpos",
    "width",
    "height",
    "ocr_text",
    "corrected_text",
    "modified",
    "hyphen_role",
}


def test_diff_projection_shape_is_pinned():
    job = _job()
    assert job.line_traces == {}  # Problem 9 — no traces needed

    diff = asyncio.run(get_job_diff(job=job))

    assert diff["job_id"] == "snap"
    assert diff["stats"] == {
        "total_lines": 10,
        "modified_lines": 0,  # no corrections applied → all identity
        "hyphen_pairs": 3,  # PART1 lines in sample.xml
    }
    assert len(diff["pages"]) == 2
    all_lines = [ln for pg in diff["pages"] for ln in pg["lines"]]
    assert len(all_lines) == 10
    for ln in all_lines:
        assert set(ln) == _LINE_KEYS
        # No correction applied → corrected mirrors ocr, not modified.
        assert ln["corrected_text"] == ln["ocr_text"]
        assert ln["modified"] is False
    for pg in diff["pages"]:
        assert set(pg) == {"page_id", "page_index", "lines"}


def test_layout_projection_shape_is_pinned():
    job = _job()

    layout = asyncio.run(get_job_layout(job=job))

    assert layout["job_id"] == "snap"
    assert len(layout["pages"]) == 2
    total_blocks = 0
    for pg in layout["pages"]:
        assert set(pg) == {
            "page_id",
            "page_index",
            "page_width",
            "page_height",
            "image_url",
            "blocks",
        }
        # Page dimensions are positive (either from ALTO or derived).
        assert pg["page_width"] > 0 and pg["page_height"] > 0
        assert pg["image_url"] is None  # no images attached
        for block in pg["blocks"]:
            total_blocks += 1
            assert set(block) == {
                "block_id",
                "hpos",
                "vpos",
                "width",
                "height",
                "lines",
            }
            for ln in block["lines"]:
                assert set(ln) == _LAYOUT_LINE_KEYS
                assert ln["corrected_text"] == ln["ocr_text"]
    assert total_blocks == 3
