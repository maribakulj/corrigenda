"""Fallback accounting counts LINES, not chunks.

``COMPLETED_WITH_FALLBACKS`` is documented (and displayed) as "N line(s)
kept their OCR source text" — so the number behind it must be a line
count. The historical counter bumped once per *chunk* that fell back: a
rejected 20-line chunk reported as "1", and a guard-rejected line
(status ``FALLBACK``, no chunk failure at all) reported as "0" while its
text silently stayed OCR.

``CorrectionResult`` now carries:

- ``fallback_lines`` — the number of lines whose terminal status is
  ``FALLBACK`` (manifest statuses are the authority);
- ``fallback_chunks`` — the old orchestration counter, renamed to say
  what it counts;
- ``fallback_reasons`` — aggregated reason prefixes for the fallen
  lines, so a consumer can say WHY without parsing messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._pipeline_harness import apply_decisions

from corrigenda.core.protocols import ProducerMetadata
from corrigenda import CorrectionPipeline, ValidationError
from corrigenda.core.editing import EditScript, ReplaceLine
from corrigenda.core.schemas import LineStatus
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        pass

    def write_trace(self, *, traces_payload):
        pass


class _AlwaysInvalidProducer:
    """Every attempt raises a recoverable validation error → every chunk
    exhausts retries/descent and falls back to OCR."""

    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    async def produce(self, payload, *, options):
        raise ValidationError("malformed on purpose")


class _OneLineGarbler:
    """Garbles exactly one non-hyphen line (same length, dissimilar text)
    so the ACCEPTANCE guard rejects it — a line-level fallback with zero
    chunk-level failure and zero retries."""

    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    def __init__(self) -> None:
        self.done = False

    async def produce(self, payload, *, options):
        ops = []
        for line in payload.lines:
            if (
                not self.done
                and line.hyphenation_role is None
                and len(line.ocr_text.split()) >= 3
            ):
                garbled = " ".join(w[::-1] for w in line.ocr_text.split())
                ops.append(ReplaceLine(line_id=line.line_id, text=garbled))
                self.done = True
        return EditScript(ops=ops), None


@pytest.mark.asyncio
async def test_rejected_multiline_chunks_count_every_line() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    total_lines = sum(len(p.lines) for p in doc.pages)
    pipeline = CorrectionPipeline(
        producer=_AlwaysInvalidProducer(),
        observer=_Null(),
        producer_metadata=ProducerMetadata(name="invalid", implementation="m"),
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )

    assert total_lines > 1
    assert result.fallback_lines == total_lines, (
        "every line of every rejected chunk must be counted, not the chunks"
    )
    assert 0 < result.fallback_chunks
    # The aggregate names the why (reason prefix → line count) and accounts
    # for every fallen line.
    assert sum(result.fallback_reasons.values()) == total_lines
    assert "all_attempts_exhausted" in result.fallback_reasons


@pytest.mark.asyncio
async def test_guard_rejection_is_a_line_fallback_without_chunk_failure() -> None:
    """A guard-rejected correction leaves the line at its OCR text: that
    IS a degraded outcome for the user, and fallback_lines says so even
    though no chunk ever failed."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        producer=_OneLineGarbler(),
        observer=_Null(),
        producer_metadata=ProducerMetadata(name="garbler", implementation="m"),
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )

    apply_decisions(doc, result)
    statuses = [lm.status for page in doc.pages for lm in page.lines]
    assert LineStatus.FALLBACK in statuses, "the guard must have rejected the garble"
    assert result.fallback_chunks == 0, "no chunk failed — this is line-level"
    assert result.fallback_lines == statuses.count(LineStatus.FALLBACK) == 1
    assert result.fallback_reasons == {"too_different_from_source": 1}


@pytest.mark.asyncio
async def test_clean_run_reports_zero_everywhere() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=_Null(),
        producer_metadata=ProducerMetadata(name="rules", implementation="v1"),
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    assert result.fallback_lines == 0
    assert result.fallback_chunks == 0
    assert result.fallback_reasons == {}
