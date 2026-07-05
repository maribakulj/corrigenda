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
from corrigenda.alto.parser import build_document_manifest
from lxml import etree

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider
from app.storage.output_writer import FilesystemOutputWriter

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
        }, None


def _run_and_capture(xml_path: Path) -> dict[str, Any]:
    """Run the orchestrator and return a snapshot of observable outputs."""
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        doc = build_document_manifest([(xml_path, xml_path.name)])
        asyncio.run(
            JobRunner(job_store=store).run(
                job_id=job_id,
                document_manifest=doc,
                provider_name="openai",
                api_key="fake-key",
                model="mock",
                output_writer=FilesystemOutputWriter(out_dir),
                source_files={xml_path.name: xml_path},
                provider=_IdentityProvider(),
            )
        )
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


# ---------------------------------------------------------------------------
# Semantic asserts (audit A6)
#
# A SHA256 snapshot trips on any byte-level change, including cosmetic
# tweaks (attribute order, whitespace) that have no behavioural impact.
# These structural asserts survive that kind of churn — when the
# SHA256 must change for a legitimate reason, these checks still
# guarantee no real invariant was broken.
# ---------------------------------------------------------------------------


def _structural_facts(xml_path: Path) -> dict[str, Any]:
    """Run the pipeline against `xml_path` and return structural facts
    extracted from the rewritten ALTO bytes."""
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        doc = build_document_manifest([(xml_path, xml_path.name)])
        asyncio.run(
            JobRunner(job_store=store).run(
                job_id=job_id,
                document_manifest=doc,
                provider_name="openai",
                api_key="fake-key",
                model="mock",
                output_writer=FilesystemOutputWriter(out_dir),
                source_files={xml_path.name: xml_path},
                provider=_IdentityProvider(),
            )
        )
        out_xml = next(out_dir.glob("*_corrected.xml"))
        out_bytes = out_xml.read_bytes()

    # Parse the SOURCE for comparison + the OUTPUT for assertions.
    src_root = etree.fromstring(xml_path.read_bytes())
    out_root = etree.fromstring(out_bytes)
    ns = "{http://www.loc.gov/standards/alto/ns-v3#}"

    def textline_ids(root: etree._Element) -> list[str]:
        return [tl.get("ID") or "" for tl in root.iter(f"{ns}TextLine")]

    def coord_map(root: etree._Element) -> dict[str, tuple[str | None, ...]]:
        return {
            tl.get("ID") or "": (
                tl.get("HPOS"),
                tl.get("VPOS"),
                tl.get("WIDTH"),
                tl.get("HEIGHT"),
            )
            for tl in root.iter(f"{ns}TextLine")
        }

    def non_hyphen_string_contents(root: etree._Element) -> dict[str, list[str | None]]:
        """For each TextLine, list the CONTENT of <String> elements that
        carry NO SUBS_TYPE attribute — i.e. plain Strings that are not
        part of a hyphen pair reconstruction.

        Under an identity-correction MockProvider, these Strings must
        round-trip byte-for-byte: the rewriter has no right to touch
        their CONTENT. Hyphen-pair Strings (PART1/PART2/BOTH) are
        legitimately rewritten — they carry SUBS_TYPE — and excluded
        from this check; the HYP-count assert in the calling test
        already guards their preservation at structural level.
        """
        out: dict[str, list[str | None]] = {}
        for tl in root.iter(f"{ns}TextLine"):
            tl_id = tl.get("ID") or ""
            out[tl_id] = [
                s.get("CONTENT") for s in tl.iter(f"{ns}String") if s.get("SUBS_TYPE") is None
            ]
        return out

    src_ids = textline_ids(src_root)
    out_ids = textline_ids(out_root)
    return {
        "textline_ids_preserved": src_ids == out_ids,
        "textline_count": len(out_ids),
        "coords_preserved": coord_map(src_root) == coord_map(out_root),
        "hyp_count": len(list(out_root.iter(f"{ns}HYP"))),
        "src_hyp_count": len(list(src_root.iter(f"{ns}HYP"))),
        "non_hyphen_string_contents_preserved": (
            non_hyphen_string_contents(src_root) == non_hyphen_string_contents(out_root)
        ),
    }


def test_semantic_sample_xml():
    """Structural invariants on sample.xml — survives cosmetic rewriter
    changes that would only trip the SHA256 snapshot."""
    facts = _structural_facts(SAMPLE_XML)
    assert facts["textline_ids_preserved"], "TextLine IDs must round-trip"
    assert facts["coords_preserved"], "TextLine coordinates must round-trip"
    assert facts["textline_count"] == 10
    # sample.xml has one explicit hyphen pair (TL4 → TL5) → 1 HYP in source
    assert facts["src_hyp_count"] == facts["hyp_count"], (
        "rewriter must preserve the HYP element count from the source"
    )
    # Roadmap L8 / T1d — non-hyphen String CONTENT must round-trip
    # under identity correction. Hyphen-pair Strings carry SUBS_TYPE
    # and are legitimately rewritten (HYP reconstruction); plain
    # Strings have no such excuse to mutate.
    assert facts["non_hyphen_string_contents_preserved"], (
        "rewriter mutated <String CONTENT> on plain (non-SUBS_TYPE) String elements "
        "under identity correction — should be a pure round-trip"
    )


@pytest.mark.skipif(not X0000002_XML.exists(), reason="X0000002.xml not in examples/")
def test_semantic_x0000002_xml():
    """Structural invariants on the large corpus."""
    facts = _structural_facts(X0000002_XML)
    assert facts["textline_ids_preserved"]
    assert facts["coords_preserved"]
    assert facts["textline_count"] == 566
    assert facts["src_hyp_count"] == facts["hyp_count"]
    # Roadmap L8 / T1d — same contract on the 566-line corpus, where
    # any silent CONTENT mutation has many more chances to surface.
    assert facts["non_hyphen_string_contents_preserved"]
