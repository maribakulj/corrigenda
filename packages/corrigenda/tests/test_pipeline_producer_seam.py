"""§5.1 resorption — the pipeline runs on any EditProducer, no credentials.

The point of the seam: a deterministic rules engine drives the WHOLE
pipeline (planner, guards, rewriter) with zero LLM, zero api_key; and a
vision producer without its images fails at start-up, before any work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._pipeline_harness import apply_decisions

from corrigenda.core.protocols import ProducerMetadata
from corrigenda import CorrectionPipeline
from corrigenda.errors import ConfigurationError
from corrigenda.core.editing import EditScript, ReplaceSpan, apply_edit_script
from corrigenda.core.protocols import ProducerOptions
from corrigenda.core.schemas import CorrectionRequest, RetryPolicy, Usage
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
        producer_metadata=ProducerMetadata(name="rules", implementation="fr-ocr-v1"),
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
    )

    # Every line with an 'e' got its first occurrences substituted; lines
    # without a rule match kept their OCR text (no fallback, no retry).
    assert result.retry_count == 0
    assert result.fallback_chunks == 0
    apply_decisions(doc, result)
    changed = [
        lm
        for page in doc.pages
        for lm in page.lines
        if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
    ]
    assert changed, "the substitution rule must have corrected something"
    for lm in changed:
        assert "3" in lm.corrected_text

    # §4 (Audit P2) — the edit_script reflects what the run ACTUALLY applied.
    # The producer's real replace_span ops survive for lines whose span
    # output was accepted unchanged; a hyphen member the reconciler rewrote
    # is surfaced as a replace_line carrying the FINAL text (never a stale
    # span claiming text the rewriter never wrote). The binding invariant:
    # replaying the edit_script over the OCR text reproduces the pipeline's
    # own final per-line text for every op'd line.
    assert result.edit_script.ops
    assert any(isinstance(op, ReplaceSpan) for op in result.edit_script.ops)
    ocr_by_line = {lm.line_id: lm.ocr_text for page in doc.pages for lm in page.lines}
    final_by_line = {
        lm.line_id: (
            lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
        )
        for page in doc.pages
        for lm in page.lines
    }
    replayed = apply_edit_script(result.edit_script, ocr_by_line)
    for op in result.edit_script.ops:
        assert replayed.text_by_id[op.line_id] == final_by_line[op.line_id]


class _VisionProducer:
    wants_geometry = True
    wants_image = True
    requires_full_coverage = False

    async def produce(
        self, payload: CorrectionRequest, *, options: ProducerOptions
    ) -> tuple[EditScript, Usage | None]:
        # Record what the compiler put in the payload, edit nothing.
        self.seen_payload = payload
        return EditScript(ops=[]), None


class _BuggyProducer:
    """Producer that raises a genuine programming error (not a provider /
    transport / validation error) on every attempt."""

    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    async def produce(self, payload, *, options):
        raise KeyError("bug in _script_to_raw")


@pytest.mark.asyncio
async def test_programming_error_propagates_not_masked_as_ocr_fallback():
    """Audit P3 — a genuine programming error on the producer path must FAIL
    the run, not be silently degraded to OCR fallback (which would report a
    'successful' run with every chunk left as uncorrected OCR). Provider
    transport / validation errors still degrade; only programmer bugs
    propagate."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        producer=_BuggyProducer(),
        observer=_Null(),
        producer_metadata=ProducerMetadata(name="buggy", implementation="m"),
    )
    with pytest.raises(KeyError):
        await pipeline.run(
            document_manifest=doc,
            source_files={_SAMPLE.name: _SAMPLE},
        )


@pytest.mark.asyncio
async def test_vision_producer_without_images_fails_at_startup():
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(producer=_VisionProducer(), observer=_Null())
    with pytest.raises(ConfigurationError, match="page_images"):
        await pipeline.run(
            document_manifest=doc,
            source_files={_SAMPLE.name: _SAMPLE},
        )


@pytest.mark.asyncio
async def test_vision_envelope_reaches_the_producer():
    """With source_images supplied, the payload carries the opaque image_ref
    and per-line geometry — copied, never opened (I4)."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    producer = _VisionProducer()
    pipeline = CorrectionPipeline(producer=producer, observer=_Null())
    await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
        page_images={page.page_id: f"opaque://{page.page_id}" for page in doc.pages},
    )
    payload = producer.seen_payload
    assert payload.image_ref is not None
    assert payload.image_ref.startswith("opaque://")
    assert all(ln.geometry is not None for ln in payload.lines)
    geo = payload.lines[0].geometry
    assert geo.page_width > 0 and geo.page_height > 0


_MULTIPAGE_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"><Layout>
<Page ID="P1" WIDTH="1000" HEIGHT="1000">
<PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
<TextLine ID="L1" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20">
<String ID="S1" CONTENT="premier" HPOS="10" VPOS="10" WIDTH="80" HEIGHT="20"/>
</TextLine></TextBlock></PrintSpace></Page>
<Page ID="P2" WIDTH="1000" HEIGHT="1000">
<PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
<TextBlock ID="B2" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
<TextLine ID="L2" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20">
<String ID="S2" CONTENT="second" HPOS="10" VPOS="10" WIDTH="80" HEIGHT="20"/>
</TextLine></TextBlock></PrintSpace></Page>
</Layout></alto>"""


class _RecordingVisionProducer:
    """Records the image_ref of EVERY payload (not just the last)."""

    wants_geometry = True
    wants_image = True
    requires_full_coverage = False

    def __init__(self):
        self.image_refs: list[str | None] = []
        self.line_ids: list[list[str]] = []

    async def produce(self, payload: CorrectionRequest, *, options: RetryPolicy):
        self.image_refs.append(payload.image_ref)
        self.line_ids.append([ln.line_id for ln in payload.lines])
        return EditScript(ops=[]), None


@pytest.mark.asyncio
async def test_multipage_file_carries_one_image_per_page(tmp_path):
    """THE per-page contract: a multipage XML has one scan per page. The
    historical per-file mapping sent page 1's image with every page's
    payload — the producer looked at the wrong scan for pages 2+."""
    src = tmp_path / "multi.xml"
    src.write_text(_MULTIPAGE_ALTO, encoding="utf-8")
    doc = build_document_manifest([(src, src.name)])
    assert [p.page_id for p in doc.pages] == ["P1", "P2"]

    producer = _RecordingVisionProducer()
    pipeline = CorrectionPipeline(producer=producer, observer=_Null())
    await pipeline.run(
        document_manifest=doc,
        source_files={src.name: src},
        page_images={"P1": "opaque://scan-1", "P2": "opaque://scan-2"},
    )

    ref_by_line = {
        lid: ref
        for ref, lids in zip(producer.image_refs, producer.line_ids)
        for lid in lids
    }
    assert ref_by_line["L1"] == "opaque://scan-1"
    assert ref_by_line["L2"] == "opaque://scan-2", (
        "page 2's payload must carry page 2's scan, never page 1's"
    )


@pytest.mark.asyncio
async def test_legacy_file_name_keys_are_refused_explicitly(tmp_path):
    """A key matching no page is almost always a pre-page_images caller
    passing file names — silently ignoring it would quietly reproduce
    the wrong-image behaviour."""
    src = tmp_path / "multi.xml"
    src.write_text(_MULTIPAGE_ALTO, encoding="utf-8")
    doc = build_document_manifest([(src, src.name)])
    pipeline = CorrectionPipeline(producer=_RecordingVisionProducer(), observer=_Null())
    with pytest.raises(ConfigurationError, match="page_id"):
        await pipeline.run(
            document_manifest=doc,
            source_files={src.name: src},
            page_images={src.name: "opaque://whole-file"},
        )
