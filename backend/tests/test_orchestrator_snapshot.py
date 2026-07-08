"""Snapshot tests for orchestrator output.

These tests assert STRUCTURAL invariants of the correction pipeline's
output on two reference corpora (a tiny one and a large one): TextLine
IDs and coordinates round-trip, HYP counts are preserved, and plain
String CONTENT is untouched under identity correction. Byte-level
parity is owned by the library's test_byte_parity_corpus (golden
sha256) — duplicating those hashes here added no signal and broke on
cosmetic lxml churn, so it was removed (audit Phase 2).

The identity-correction MockProvider returns each line's OCR text
unchanged, so the output should be a deterministic transformation of
the input that depends only on the parser/rewriter/reconciler logic.

If a snapshot must change (e.g. an intentional rewriter improvement),
update the constants below in the same commit and justify it in the
commit message.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest
from corrigenda.formats.alto.parser import build_document_manifest
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
