"""Tests for line_acceptance — centralized accept/fallback policy (Sprint 7)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from corrigenda.core.guards import (
    check_adjacent_duplicates,
    check_line,
)
from corrigenda.formats.alto.parser import build_document_manifest, parse_alto_file

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider
from app.storage import init_job_dirs, output_dir, save_uploaded_files
from app.storage.output_writer import FilesystemOutputWriter

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"
X0000002_PATH = Path(__file__).parent.parent.parent / "examples" / "X0000002.xml"


# ===========================================================================
# Unit tests for check_line
# ===========================================================================


class TestCheckLineAccepted:
    """Test 1: reasonable corrections are accepted."""

    def test_identical_always_accepted(self):
        r = check_line("Bonjour le monde", "Bonjour le monde")
        assert r.accepted
        assert r.text == "Bonjour le monde"
        assert r.reason is None

    def test_minor_correction_accepted(self):
        r = check_line(
            "la municipalité prenne les rnesures néces-",
            "la municipalité prenne les mesures néces-",
        )
        assert r.accepted
        assert r.text == "la municipalité prenne les mesures néces-"

    def test_ocr_typo_fix_accepted(self):
        r = check_line(
            "HISTOIRE DE LA RÉVOLUTIOM",
            "HISTOIRE DE LA RÉVOLUTION",
        )
        assert r.accepted

    def test_single_word_fix_accepted(self):
        r = check_line("Le clieval court vite", "Le cheval court vite")
        assert r.accepted

    def test_punctuation_fix_accepted(self):
        r = check_line("Fin du texte,", "Fin du texte.")
        assert r.accepted


class TestCheckLineTooFar:
    """Test 2: corrections too far from source are rejected."""

    def test_totally_different_rejected(self):
        r = check_line(
            "la municipalité prenne les mesures",
            "ABCDEFG HIJKLMNOP QRSTUV WXYZ",
        )
        assert not r.accepted
        assert r.text == "la municipalité prenne les mesures"
        assert r.reason == "too_different_from_source"

    def test_replacement_by_unrelated_text_rejected(self):
        r = check_line(
            "Article premier. Les biens ecclésiastiques",
            "The quick brown fox jumps over the lazy dog",
        )
        assert not r.accepted
        assert r.reason == "too_different_from_source"


class TestCheckLineNeighbourPrevious:
    """Test 3: correction closer to previous line is rejected."""

    def test_closer_to_prev_line_rejected(self):
        source = "saires pour que les chemins"
        prev_ocr = "la municipalité prenne les mesures néces-"
        # Correction looks like prev line (migration)
        corrected = "la municipalité prenne les mesures néces-"
        r = check_line(source, corrected, prev_ocr=prev_ocr)
        assert not r.accepted
        assert r.reason == "closer_to_previous_line"

    def test_not_rejected_when_similar_to_prev_but_also_to_source(self):
        """If the correction is equally close to source and prev, accept it."""
        source = "les mesures nécessaires pour"
        prev_ocr = "les mesures préliminaires pour"
        corrected = "les mesures nécessaires pour"  # same as source
        r = check_line(source, corrected, prev_ocr=prev_ocr)
        assert r.accepted


class TestCheckLineNeighbourNext:
    """Test 4: correction closer to next line is rejected."""

    def test_closer_to_next_line_rejected(self):
        source = "la municipalité prenne les mesures néces-"
        next_ocr = "la municipalité ordonne les mesures suivantes"
        # Correction is tweaked to look more like next_ocr than source
        corrected = "la municipalité ordonne les mesures suivantes"
        r = check_line(source, corrected, next_ocr=next_ocr)
        assert not r.accepted
        assert r.reason == "closer_to_next_line"


# ===========================================================================
# Test: absorption guard
# ===========================================================================


class TestAbsorption:
    """Guard 3: correction absorbs adjacent line content."""

    def test_absorbs_next_line_detected(self):
        """TL000024 pattern: source + suffix from next line → rejected."""
        source = "cou s'ils y vont en voiture, à se donner une"
        next_ocr = "entorse s'ils y vont à pieds."
        corrected = "cou s'ils y vont en voiture, à se donner une entorse s'ils y vont à pied."
        r = check_line(source, corrected, next_ocr=next_ocr)
        assert not r.accepted
        assert r.reason == "absorbs_next_line"
        assert r.text == source

    def test_absorbs_next_line_with_correction(self):
        """Even if the LLM also corrects typos, absorption is caught."""
        source = "la municipalité prenne les rnesures néces-"
        next_ocr = "saires pour que les chemins soient"
        corrected = "la municipalité prenne les mesures nécessaires pour que les chemins soient"
        r = check_line(source, corrected, next_ocr=next_ocr)
        assert not r.accepted
        assert r.reason == "absorbs_next_line"

    def test_normal_correction_not_flagged_as_absorption(self):
        """A minor OCR fix on a line must NOT trigger absorption."""
        source = "cou s'ils y vont en voiture, à se donner une"
        next_ocr = "entorse s'ils y vont à pieds."
        corrected = "cou s'ils y vont en voiture, à se donner une"
        r = check_line(source, corrected, next_ocr=next_ocr)
        assert r.accepted

    def test_slightly_longer_correction_not_absorption(self):
        """Adding a missing word is NOT absorption if it doesn't match next line."""
        source = "la municipalité prenne les mesures"
        next_ocr = "nécessaires pour que les chemins soient"
        corrected = "la municipalité prenne les mesures urgentes"  # adds 1 word
        r = check_line(source, corrected, next_ocr=next_ocr)
        assert r.accepted

    def test_absorbs_previous_line_detected(self):
        """Symmetric case: correction absorbs previous line as prefix."""
        source = "saires pour que les chemins soient"
        prev_ocr = "la municipalité prenne les mesures néces-"
        corrected = "la municipalité prenne les mesures nécessaires pour que les chemins soient"
        r = check_line(source, corrected, prev_ocr=prev_ocr)
        assert not r.accepted
        assert r.reason == "absorbs_previous_line"

    def test_no_absorption_when_next_ocr_absent(self):
        """Without neighbour context, absorption cannot be checked."""
        source = "cou s'ils y vont en voiture, à se donner une"
        corrected = "cou s'ils y vont en voiture, à se donner une entorse s'ils y vont à pied."
        # No next_ocr → guard 1 (source similarity) or accept
        r = check_line(source, corrected)
        # Should be handled by source similarity or accepted — not absorption
        assert r.reason != "absorbs_next_line"

    def test_duplication_scenario_end_to_end(self):
        """
        Full scenario: line i absorbs line i+1.
        Line i should be rejected (absorption).
        Line i+1 corrected badly → rejected by source similarity.
        No duplication in final output.
        """
        source_i = "cou s'ils y vont en voiture, à se donner une"
        source_next = "entorse s'ils y vont à pieds."
        corrected_i = "cou s'ils y vont en voiture, à se donner une entorse s'ils y vont à pied."
        corrected_next = "G."  # LLM hallucinated

        r_i = check_line(source_i, corrected_i, next_ocr=source_next)
        assert not r_i.accepted
        assert r_i.reason == "absorbs_next_line"
        assert r_i.text == source_i  # falls back to OCR

        r_next = check_line(source_next, corrected_next, prev_ocr=source_i)
        assert not r_next.accepted
        assert r_next.reason == "too_different_from_source"
        assert r_next.text == source_next  # falls back to OCR


class TestAdjacentDuplicates:
    """Test 5: adjacent duplicate detection."""

    def test_duplicate_corrections_flagged(self):
        lines = [
            (
                "L1",
                "la municipalité prenne les mesures",
                "la municipalité prenne les mesures nécessaires",
            ),
            (
                "L2",
                "saires pour que les chemins soient",
                "la municipalité prenne les mesures nécessaires",
            ),
        ]
        reverts = check_adjacent_duplicates(lines)
        assert "L1" in reverts or "L2" in reverts
        assert (
            reverts.get("L1") == "adjacent_duplicate_detected"
            or reverts.get("L2") == "adjacent_duplicate_detected"
        )

    def test_same_source_not_flagged(self):
        """If sources are already similar, identical corrections are OK."""
        lines = [
            ("L1", "le texte est identique", "le texte est identique"),
            ("L2", "le texte est identique", "le texte est identique"),
        ]
        reverts = check_adjacent_duplicates(lines)
        assert len(reverts) == 0

    def test_different_corrections_not_flagged(self):
        lines = [
            ("L1", "première ligne source", "première ligne corrigée"),
            ("L2", "deuxième ligne source", "deuxième ligne corrigée"),
        ]
        reverts = check_adjacent_duplicates(lines)
        assert len(reverts) == 0


# ===========================================================================
# Test 6: Trace key robustness with repeated line_ids across pages
# ===========================================================================


class TestTraceKeyCollision:
    def test_multi_page_trace_keys_no_collision(self):
        """Two pages with the same line_ids produce separate traces."""
        if not SAMPLE_XML.exists():
            pytest.skip("sample.xml not available")

        # Build two copies of the same XML as separate "pages"
        xml_bytes = SAMPLE_XML.read_bytes()

        store = JobStore()
        job_id = store.create_job(Provider.OPENAI, "mock")
        init_job_dirs(job_id)

        saved, _ = save_uploaded_files(
            job_id,
            [("page1.xml", xml_bytes), ("page2.xml", xml_bytes)],
        )
        doc = build_document_manifest([(p, n) for n, p in saved.items()])
        store.update_job(job_id, document_manifest=doc)

        # Must have pages from both files with overlapping line_ids
        assert len(doc.pages) >= 2
        file1_ids = {
            lm.line_id for p in doc.pages if p.source_file == "page1.xml" for lm in p.lines
        }
        file2_ids = {
            lm.line_id for p in doc.pages if p.source_file == "page2.xml" for lm in p.lines
        }
        overlap = file1_ids & file2_ids
        assert len(overlap) > 0, "Expected overlapping line IDs from two copies of same XML"

        # Run with identity provider
        class IdentityProvider:
            async def list_models(self, api_key):
                return [ModelInfo(id="mock", label="Mock")]

            async def complete_structured(self, **kwargs):
                return {
                    "lines": [
                        {"line_id": l["line_id"], "corrected_text": l["ocr_text"]}
                        for l in kwargs.get("user_payload", {}).get("lines", [])
                    ]
                }, None

        out_dir = output_dir(job_id)
        asyncio.run(
            JobRunner(job_store=store).run(
                job_id=job_id,
                document_manifest=doc,
                provider_name="openai",
                api_key="fake-key",
                model="mock",
                output_writer=FilesystemOutputWriter(out_dir),
                source_files={n: p for n, p in saved.items()},
                provider=IdentityProvider(),
            )
        )

        job = store.get_job(job_id)
        traces = job.report.lines

        # Total traces should equal total lines across both pages (no collision)
        total_lines = sum(len(p.lines) for p in doc.pages)
        assert len(traces) == total_lines, (
            f"Expected {total_lines} traces, got {len(traces)} (collision between pages?)"
        )

        # All traces should have 5 text states
        for t in traces:
            assert t.source_ocr_text is not None
            assert t.output_alto_text is not None


# ===========================================================================
# Integration: line_acceptance in the real pipeline
# ===========================================================================


class TestLineAcceptanceIntegration:
    def test_migration_provider_triggers_fallback(self):
        """A provider that shifts line content triggers line_acceptance fallback."""
        if not SAMPLE_XML.exists():
            pytest.skip("sample.xml not available")

        pages, _ = parse_alto_file(SAMPLE_XML, "sample.xml")
        lines = pages[0].lines
        if len(lines) < 2:
            pytest.skip("Need at least 2 lines")

        # Provider that swaps line 0 and line 1 content
        class SwapProvider:
            async def list_models(self, api_key):
                return [ModelInfo(id="mock", label="Mock")]

            async def complete_structured(self, **kwargs):
                payload_lines = kwargs.get("user_payload", {}).get("lines", [])
                out = []
                id_to_text = {l["line_id"]: l["ocr_text"] for l in payload_lines}
                ids = [l["line_id"] for l in payload_lines]
                for l in payload_lines:
                    lid = l["line_id"]
                    # Swap first two lines
                    if lid == lines[0].line_id and lines[1].line_id in id_to_text:
                        out.append({"line_id": lid, "corrected_text": id_to_text[lines[1].line_id]})
                    elif lid == lines[1].line_id and lines[0].line_id in id_to_text:
                        out.append({"line_id": lid, "corrected_text": id_to_text[lines[0].line_id]})
                    else:
                        out.append({"line_id": lid, "corrected_text": l["ocr_text"]})
                return {"lines": out}, None

        store = JobStore()
        job_id = store.create_job(Provider.OPENAI, "mock")
        init_job_dirs(job_id)
        saved, _ = save_uploaded_files(job_id, [("sample.xml", SAMPLE_XML.read_bytes())])
        doc = build_document_manifest([(p, n) for n, p in saved.items()])
        store.update_job(job_id, document_manifest=doc)

        out_dir = output_dir(job_id)
        asyncio.run(
            JobRunner(job_store=store).run(
                job_id=job_id,
                document_manifest=doc,
                provider_name="openai",
                api_key="fake-key",
                model="mock",
                output_writer=FilesystemOutputWriter(out_dir),
                source_files={n: p for n, p in saved.items()},
                provider=SwapProvider(),
            )
        )

        job = store.get_job(job_id)
        by_line = {t.line_id: t for t in job.report.lines}

        # The swapped lines should have been caught and fallen back
        t0 = by_line[lines[0].line_id]
        t1 = by_line[lines[1].line_id]

        # At least one should have been caught (depends on similarity)
        swapped = [t for t in [t0, t1] if t.validation_status == "fallback"]
        assert len(swapped) > 0, (
            f"Expected at least one swapped line to be caught. "
            f"t0.status={t0.validation_status}, t1.status={t1.validation_status}"
        )

        # Check that fallback reasons are from line_acceptance
        for t in swapped:
            assert t.fallback_reason in (
                "closer_to_previous_line",
                "closer_to_next_line",
                "too_different_from_source",
                "adjacent_duplicate_detected",
            ), f"Unexpected fallback_reason: {t.fallback_reason}"
