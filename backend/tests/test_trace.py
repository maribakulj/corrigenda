"""Tests for Sprint 5bis — line trace pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from alto_core.alto.parser import build_document_manifest, parse_alto_file
from alto_core.alto.rewriter import extract_output_texts, rewrite_alto_file

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import (
    HyphenRole,
    JobTrace,
    LineTrace,
    ModelInfo,
    Provider,
)
from app.storage import (
    init_job_dirs,
    output_dir,
    save_uploaded_files,
)
from app.storage.output_writer import FilesystemOutputWriter

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"
X0000002_PATH = Path(__file__).parent.parent.parent / "examples" / "X0000002.xml"

NS = "http://www.loc.gov/standards/alto/ns-v3#"


# ---------------------------------------------------------------------------
# Mock providers
# ---------------------------------------------------------------------------


class IdentityProvider:
    """Returns each line's OCR text unchanged."""

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
                {"line_id": l["line_id"], "corrected_text": l["ocr_text"]}
                for l in user_payload.get("lines", [])
            ]
        }


class CorrectionProvider:
    """Applies a fixed correction map; returns OCR text for unmapped lines."""

    def __init__(self, corrections: dict[str, str]) -> None:
        self._corrections = corrections

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
        lines_out = []
        for l in user_payload.get("lines", []):
            lid = l["line_id"]
            text = self._corrections.get(lid, l["ocr_text"])
            lines_out.append({"line_id": lid, "corrected_text": text})
        return {"lines": lines_out}


class DriftProvider:
    """Returns grossly inflated text to trigger drift guard fallback."""

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
                {
                    "line_id": l["line_id"],
                    "corrected_text": l["ocr_text"] + " EXTRA " * 100,
                }
                for l in user_payload.get("lines", [])
            ]
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _run_job_with_traces(
    source_bytes: dict[str, bytes],
    provider=None,
) -> tuple[str, dict[str, LineTrace]]:
    """Run a job and return (job_id, traces_dict)."""
    if provider is None:
        provider = IdentityProvider()

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
            provider=provider,
        )
    )

    job = store.get_job(job_id)
    # Build a line_id-keyed view (traces use composite page_id:line_id keys internally)
    by_line_id = {t.line_id: t for t in job.line_traces.values()}
    return job_id, by_line_id


# ===========================================================================
# Test 1: Normal line produces all 5 text states
# ===========================================================================


class TestTraceNormalLine:
    def test_identity_line_has_5_states(self):
        """A normal unchanged line produces all 5 text states."""
        job_id, traces = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
        )
        assert len(traces) > 0

        # Pick any line
        t = next(iter(traces.values()))
        assert t.source_ocr_text is not None
        assert t.model_input_text is not None
        assert t.model_corrected_text is not None
        assert t.projected_text is not None
        assert t.output_alto_text is not None

    def test_unchanged_line_texts_equal(self):
        """For an unchanged line: source == projected == output."""
        job_id, traces = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
        )
        for t in traces.values():
            assert t.source_ocr_text == t.projected_text, f"{t.line_id}: source_ocr != projected"
            assert t.source_ocr_text == t.output_alto_text, (
                f"{t.line_id}: source_ocr != output_alto"
            )

    def test_rewriter_path_set(self):
        """Each line has a rewriter_path."""
        job_id, traces = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
        )
        for t in traces.values():
            assert t.rewriter_path in (
                "untouched",
                "subs_only",
                "fast_path",
                "slow_path",
            ), f"{t.line_id}: bad rewriter_path={t.rewriter_path}"


# ===========================================================================
# Test 2: Correction accepted — model_corrected differs from source
# ===========================================================================


class TestTraceCorrectionAccepted:
    def test_correction_distinguished(self):
        """A corrected line distinguishes source, model_corrected, projected."""
        # Parse sample to get a line_id
        pages, _ = parse_alto_file(SAMPLE_XML, "sample.xml")
        first_line = pages[0].lines[0]

        # Use a realistic correction (small change, high similarity to source)
        corrected_text = first_line.ocr_text.replace("RÉVOLUTIOM", "RÉVOLUTION")
        corrections = {first_line.line_id: corrected_text}
        job_id, traces = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
            provider=CorrectionProvider(corrections),
        )
        t = traces[first_line.line_id]

        assert t.source_ocr_text == first_line.ocr_text
        assert t.model_corrected_text == corrected_text
        assert t.projected_text == corrected_text
        assert t.validation_status == "corrected"


# ===========================================================================
# Test 3: Drift guard fallback
# ===========================================================================


class TestTraceFallback:
    def test_drift_fallback_distinguished(self):
        """A drift-guarded line shows model_corrected != projected."""
        job_id, traces = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
            provider=DriftProvider(),
        )
        for t in traces.values():
            # model_corrected should be the inflated text
            assert "EXTRA" in (t.model_corrected_text or ""), (
                f"{t.line_id}: model_corrected should contain drift text"
            )
            # projected should be the original OCR (fallback)
            assert t.projected_text == t.source_ocr_text, (
                f"{t.line_id}: projected should fall back to source"
            )
            assert t.validation_status == "fallback"
            assert t.fallback_reason is not None
            # The drift can be caught by the chunk-level validator
            # (all_attempts_exhausted after downgrade) or, for non-hyphen
            # lines whose LLM call succeeds, by the per-line acceptance
            # guard. F1 — granularity downgrade means a drifting non-hyphen
            # line is now individually rejected by check_line (reason
            # "too_different_from_source" / neighbour / absorption) instead
            # of being collateral in a whole-chunk hyphen fallback.
            accepted_reasons = (
                "drift_guard",
                "all_attempts_exhausted",
                "too_different_from_source",
                "closer_to_",
                "absorbs_",
            )
            assert any(r in t.fallback_reason for r in accepted_reasons), (
                f"{t.line_id}: unexpected fallback_reason {t.fallback_reason!r}"
            )


# ===========================================================================
# Test 4: trace.json file written
# ===========================================================================


class TestTraceJsonFile:
    def test_trace_json_exists(self):
        """trace.json is written to the output directory."""
        job_id, _ = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
        )
        trace_path = output_dir(job_id) / "trace.json"
        assert trace_path.exists()

        data = json.loads(trace_path.read_text())
        assert data["job_id"] == job_id
        assert data["total_lines"] > 0
        assert len(data["lines"]) == data["total_lines"]

    def test_trace_json_roundtrip(self):
        """trace.json can be parsed back into JobTrace."""
        job_id, _ = _run_job_with_traces(
            {"sample.xml": SAMPLE_XML.read_bytes()},
        )
        trace_path = output_dir(job_id) / "trace.json"
        jt = JobTrace.model_validate_json(trace_path.read_text())
        assert jt.job_id == job_id
        assert len(jt.lines) > 0


# ===========================================================================
# Test 5: extract_output_texts consistency
# ===========================================================================


class TestExtractOutputTexts:
    def test_no_double_dash(self):
        """output_alto_text doesn't introduce double-dash for HYP lines."""
        if not X0000002_PATH.exists():
            pytest.skip("X0000002.xml not available")

        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        xml_bytes, _metrics, _paths = rewrite_alto_file(
            X0000002_PATH,
            pages,
            "test",
            "test-model",
        )

        hyp_lines = {
            lm.line_id
            for page in pages
            for lm in page.lines
            if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
        }
        output_texts = extract_output_texts(xml_bytes, hyp_lines)

        for lid, text in output_texts.items():
            assert not text.endswith("--"), f"{lid}: output_alto_text ends with '--' (double dash)"

    def test_extract_matches_source_for_untouched(self):
        """For untouched lines, extracted output matches source OCR text."""
        if not X0000002_PATH.exists():
            pytest.skip("X0000002.xml not available")

        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        all_ids = {lm.line_id for page in pages for lm in page.lines}

        # Rewrite without corrections (identity)
        xml_bytes, _m, _p = rewrite_alto_file(
            X0000002_PATH,
            pages,
            "test",
            "test-model",
        )
        output_texts = extract_output_texts(xml_bytes, all_ids)

        line_by_id = {lm.line_id: lm for page in pages for lm in page.lines}
        for lid, otxt in output_texts.items():
            lm = line_by_id[lid]
            assert otxt == lm.ocr_text, (
                f"{lid}: output_alto_text {otxt!r} != source {lm.ocr_text!r}"
            )


# ===========================================================================
# Test 6: Hyphen and BOTH lines produce coherent traces
# ===========================================================================


class TestTraceHyphenLines:
    def test_hyphen_traces_on_corpus(self):
        """Hyphen and BOTH lines on X0000002 get correct trace metadata."""
        if not X0000002_PATH.exists():
            pytest.skip("X0000002.xml not available")

        job_id, traces = _run_job_with_traces(
            {"X0000002.xml": X0000002_PATH.read_bytes()},
        )

        # Check that traces exist for all lines
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        total_lines = sum(len(p.lines) for p in pages)
        assert len(traces) == total_lines

        # All traces have 5 states
        for t in traces.values():
            assert t.source_ocr_text is not None
            assert t.model_input_text is not None
            assert t.model_corrected_text is not None
            assert t.projected_text is not None
            assert t.output_alto_text is not None

        # PART1 and BOTH lines have correct hyphen_role
        line_by_id = {lm.line_id: lm for page in pages for lm in page.lines}
        part1_traces = [t for t in traces.values() if t.hyphen_role == "HypPart1"]
        both_traces = [t for t in traces.values() if t.hyphen_role == "HypBoth"]
        assert len(part1_traces) > 0
        assert len(both_traces) > 0

        # Specific check: néces-/saires pair (TL000014)
        nec = traces.get("PAG_00000002_TL000014")
        if nec is not None:
            assert nec.hyphen_role == "HypPart1"
            assert "néces-" in nec.source_ocr_text
            assert nec.projected_text == nec.source_ocr_text  # identity provider

        # Specific check: TL000017 is BOTH
        tl17 = traces.get("PAG_00000002_TL000017")
        if tl17 is not None:
            assert tl17.hyphen_role == "HypBoth"

    def test_unchanged_hyphen_output_matches_source(self):
        """For identity correction, hyphen output_alto_text == source_ocr_text."""
        if not X0000002_PATH.exists():
            pytest.skip("X0000002.xml not available")

        job_id, traces = _run_job_with_traces(
            {"X0000002.xml": X0000002_PATH.read_bytes()},
        )
        for t in traces.values():
            assert t.source_ocr_text == t.output_alto_text, (
                f"{t.line_id}: source={t.source_ocr_text!r} != output={t.output_alto_text!r}"
            )
