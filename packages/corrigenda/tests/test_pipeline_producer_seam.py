"""§5.1 resorption — the pipeline runs on any EditProducer, no credentials.

The point of the seam: a deterministic rules engine drives the WHOLE
pipeline (planner, guards, rewriter) with zero LLM, zero api_key; and a
vision producer without its images fails at start-up, before any work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline, ValidationError
from corrigenda.core.editing import EditScript, ReplaceSpan
from corrigenda.core.schemas import LLMUserPayload, RetryPolicy, Usage
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


@pytest.mark.asyncio
async def test_rules_producer_drives_full_pipeline_without_credentials():
    """A deterministic producer corrects a document end-to-end: replace_span
    ops flow through E1–E5, uncovered lines stay identity (no error), and
    the accumulated edit_script surfaces the producer's real span ops."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    producer = RulesProducer([SubstitutionRule("e", "3")])

    pipeline = CorrectionPipeline(
        producer=producer,
        observer=_Null(),
        output_writer=_Null(),
        provider_name="rules",
        model="fr-ocr-v1",
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
        apply=False,
    )

    # Every line with an 'e' got its first occurrences substituted; lines
    # without a rule match kept their OCR text (no fallback, no retry).
    assert result.retry_count == 0
    assert result.fallback_count == 0
    changed = [
        lm
        for page in doc.pages
        for lm in page.lines
        if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
    ]
    assert changed, "the substitution rule must have corrected something"
    for lm in changed:
        assert "3" in lm.corrected_text

    # The run's edit_script carries the producer's actual ReplaceSpan ops.
    assert result.edit_script.ops
    assert all(isinstance(op, ReplaceSpan) for op in result.edit_script.ops)


class _VisionProducer:
    wants_geometry = True
    wants_image = True
    requires_full_coverage = False

    async def produce(
        self, payload: LLMUserPayload, *, policy: RetryPolicy
    ) -> tuple[EditScript, Usage | None]:
        # Record what the compiler put in the payload, edit nothing.
        self.seen_payload = payload
        return EditScript(ops=[]), None


@pytest.mark.asyncio
async def test_vision_producer_without_images_fails_at_startup():
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        producer=_VisionProducer(), observer=_Null(), output_writer=_Null()
    )
    with pytest.raises(ValidationError, match="source_images"):
        await pipeline.run(
            document_manifest=doc,
            source_files={_SAMPLE.name: _SAMPLE},
            apply=False,
        )


@pytest.mark.asyncio
async def test_vision_envelope_reaches_the_producer():
    """With source_images supplied, the payload carries the opaque image_ref
    and per-line geometry — copied, never opened (I4)."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    producer = _VisionProducer()
    pipeline = CorrectionPipeline(
        producer=producer, observer=_Null(), output_writer=_Null()
    )
    await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
        source_images={_SAMPLE.name: "opaque://page-1"},
        apply=False,
    )
    payload = producer.seen_payload
    assert payload.image_ref == "opaque://page-1"
    assert all(ln.geometry is not None for ln in payload.lines)
    geo = payload.lines[0].geometry
    assert geo.page_width > 0 and geo.page_height > 0
