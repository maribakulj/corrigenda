"""Every line gets exactly one terminal decision — enforced, not assumed.

ADR-008 lets the page loop absorb a *recoverable* ``CorrectionError``
from a chunk as a ``chunk_error`` event and continue. Absorbing the
error is right; absorbing it while the chunk's target lines are still
``PENDING`` is not — the run would end "successfully" with lines nobody
decided, and the report would silently carry them as if they had been
processed.

Two layers pin the contract:

1. the absorb branch OCR-falls-back every still-undecided target line of
   the failed chunk before continuing (lines the chunk already finalized
   keep their decision);
2. a final run-level check refuses to write outputs while any line is
   ``PENDING`` — a violation is an engine bug and fails the run loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline, ValidationError
from corrigenda.core.decisions import derive_decision_set
from corrigenda.core.schemas import LineStatus, PipelineEventType
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _EventLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def on_event(self, event_type, payload):
        self.events.append((event_type, payload))

    def write_corrected(self, *, source_stem, xml_bytes):
        pass

    def write_trace(self, *, traces_payload):
        pass


def _pipeline(observer) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=observer,
        provider_name="rules",
        model="v1",
    )


@pytest.mark.asyncio
async def test_absorbed_chunk_error_leaves_no_line_undecided(monkeypatch) -> None:
    """A recoverable CorrectionError escaping the chunk (reconcile /
    finalize path, after the producer attempt loop) is absorbed — but
    every target line of that chunk must still end in a terminal state."""

    def _bomb(self, **kwargs):
        raise ValidationError("simulated reconcile-path failure")

    monkeypatch.setattr(CorrectionPipeline, "_finish_successful_chunk", _bomb)

    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    observer = _EventLog()
    result = await _pipeline(observer).run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
    )

    statuses = {
        (d.ref.page_id, d.ref.line_id): d.status for d in result.decisions.decisions
    }
    undecided = [k for k, s in statuses.items() if s is LineStatus.PENDING]
    assert undecided == [], (
        f"absorbed chunk errors left {len(undecided)} line(s) with no "
        f"terminal decision: {undecided[:5]}"
    )
    # The absorb semantics are preserved: the run completed, said what
    # happened, and accounted for the degradation.
    assert any(e == PipelineEventType.CHUNK_ERROR for e, _ in observer.events)
    assert all(s is LineStatus.FALLBACK for s in statuses.values())
    assert result.fallback_chunks > 0
    # Fallback lines carry their source text — never None, never invented.
    assert all(d.final_text == d.source_text for d in result.decisions.decisions)


@pytest.mark.asyncio
async def test_partial_decisions_survive_the_absorb(monkeypatch) -> None:
    """Only STILL-UNDECIDED lines fall back: a line the failing chunk (or
    an earlier chunk) already finalized keeps its correction."""
    real = CorrectionPipeline._finish_successful_chunk
    calls = {"n": 0}

    def _second_call_bombs(self, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ValidationError("simulated late-chunk failure")
        return real(self, **kwargs)

    monkeypatch.setattr(
        CorrectionPipeline, "_finish_successful_chunk", _second_call_bombs
    )

    # Tiny windows → several chunks per page, so call #1 succeeds and
    # later chunks fail.
    from corrigenda.core.schemas import ChunkPlannerConfig

    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    observer = _EventLog()
    pipeline = CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("e", "3")]),
        observer=observer,
        config=ChunkPlannerConfig(
            max_input_chars_per_request=30,
            max_lines_per_request=2,
            line_window_size=2,
            line_window_overlap=1,
        ),
        provider_name="rules",
        model="v1",
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )

    statuses = [d.status for d in result.decisions.decisions]
    assert LineStatus.PENDING not in statuses
    assert LineStatus.CORRECTED in statuses, "the first chunk's decisions survive"
    assert LineStatus.FALLBACK in statuses, "the failed chunks' lines fell back"


def test_run_level_invariant_names_the_pending_line() -> None:
    """ADR-011 — the backstop IS the DecisionSet's construction invariant:
    an undecided line refuses materialization, so no decisions (and no
    outputs) can exist for a document with a forgotten line."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    # All lines PENDING (freshly parsed): materialization must refuse.
    with pytest.raises(RuntimeError, match="PENDING"):
        derive_decision_set(doc, {})
    # After deciding every line, the set materializes and reflects it.
    total = 0
    for page in doc.pages:
        for lm in page.lines:
            lm.status = LineStatus.FALLBACK
            lm.corrected_text = lm.ocr_text
            total += 1
    decisions = derive_decision_set(doc, {})
    assert len(decisions.decisions) == total
    assert decisions.fallback_lines == total
    assert decisions.fallback_reason_counts() == {"unspecified": total}
