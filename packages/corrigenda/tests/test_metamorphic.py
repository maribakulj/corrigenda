"""Metamorphic properties over the WHOLE pipeline (plan → produce →
validate → reconcile → finalize).

``test_properties_hypothesis.py`` pins parser/planner/rewriter invariants
in isolation; these properties run the complete engine with a
deterministic producer and assert the behavioural contracts any internal
restructuring must preserve:

1. **Chunking invariance** — the final text and status of every line is
   independent of the chunk partition. A deterministic producer must
   yield identical corrections whether the planner cut the document into
   pages, windows or near-single lines. This is the executable definition
   of "the seam passes are correct": duplicate detection, finalization
   ownership and boundary handling may not let the partition show through
   to the result.
2. **Identity producer** — a producer that proposes nothing leaves every
   String CONTENT and every TextLine's identity/geometry untouched in the
   rewritten XML.
3. **Run independence** — two runs over manifests parsed from the same
   file do not contaminate each other; the same configuration yields the
   same result every time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from hypothesis import given, settings

from corrigenda import CorrectionPipeline
from corrigenda.core.editing import EditScript
from corrigenda.core.schemas import ChunkPlannerConfig, DocumentManifest, LineStatus
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

from tests.test_properties_hypothesis import (
    _string_contents,
    _textline_geometry,
    _write_tmp,
    alto_documents,
)

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"

# Three partitions of the same document: whole-page plans, overlapping
# 2-line windows, and 3-line windows. Budgets are in characters, so the
# tiny configs force WINDOW/LINE granularity on any non-trivial page.
_PARTITIONS: dict[str, ChunkPlannerConfig | None] = {
    "default": None,
    "tiny-window": ChunkPlannerConfig(
        max_input_chars_per_request=30,
        max_lines_per_request=2,
        line_window_size=2,
        line_window_overlap=1,
    ),
    "small-window": ChunkPlannerConfig(
        max_input_chars_per_request=120,
        max_lines_per_request=3,
        line_window_size=3,
        line_window_overlap=1,
    ),
}

# Substitutions over common letters so most generated documents get at
# least one real correction; determinism, not coverage, is the point.
_RULES = [
    SubstitutionRule("e", "3"),
    SubstitutionRule("a", "4"),
    SubstitutionRule("o", "0"),
]


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        pass

    def write_trace(self, *, traces_payload):
        pass


class _Capture:
    def __init__(self) -> None:
        self.outputs: dict[str, bytes] = {}

    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        self.outputs[source_stem] = xml_bytes

    def write_trace(self, *, traces_payload):
        pass


class _IdentityProducer:
    """Proposes nothing — every line must come out exactly as it went in."""

    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    async def produce(self, payload, *, policy):
        return EditScript(ops=[]), None


def _outcomes(doc: DocumentManifest) -> dict[tuple[str, str], tuple[str, str]]:
    """(page_id, line_id) → (final text, status) for the whole document."""
    return {
        (lm.page_id, lm.line_id): (
            lm.corrected_text if lm.corrected_text is not None else lm.ocr_text,
            lm.status.value,
        )
        for page in doc.pages
        for lm in page.lines
    }


async def _run_partition(
    path: Path, config: ChunkPlannerConfig | None
) -> DocumentManifest:
    doc = build_document_manifest([(path, path.name)])
    pipeline = CorrectionPipeline(
        producer=RulesProducer(_RULES),
        observer=_Null(),
        output_writer=_Null(),
        config=config,
        provider_name="rules",
        model="fr-ocr-v1",
    )
    await pipeline.run(
        document_manifest=doc,
        source_files={path.name: path},
        apply=False,
    )
    return doc


# ---------------------------------------------------------------------------
# Property 1 — chunking invariance
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None)
@given(doc=alto_documents())
def test_final_text_is_invariant_under_chunk_partition(doc: str) -> None:
    path = _write_tmp(doc)
    try:
        results = {
            name: _outcomes(asyncio.run(_run_partition(path, config)))
            for name, config in _PARTITIONS.items()
        }
        baseline = results["default"]
        assert set(baseline) == {key for r in results.values() for key in r}, (
            "every partition must decide the same set of lines"
        )
        assert all(
            status != LineStatus.PENDING.value for text, status in baseline.values()
        ), "no line may end a run undecided"
        for name, outcome in results.items():
            assert outcome == baseline, (
                f"partition {name!r} changed the result: the chunk plan "
                "leaked into the corrected text or statuses"
            )
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_final_text_is_invariant_under_chunk_partition_on_sample() -> None:
    """Fixed-corpus anchor for the property above (fast, deterministic)."""
    results = {
        name: _outcomes(await _run_partition(_SAMPLE, config))
        for name, config in _PARTITIONS.items()
    }
    baseline = results["default"]
    corrected = [
        k for k, (_, status) in baseline.items() if status == LineStatus.CORRECTED.value
    ]
    assert corrected, "the rules must have produced at least one correction"
    for name, outcome in results.items():
        assert outcome == baseline, f"partition {name!r} changed the result"


# ---------------------------------------------------------------------------
# Property 2 — identity producer leaves content and geometry untouched
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None)
@given(doc=alto_documents())
def test_identity_producer_preserves_content_and_geometry(doc: str) -> None:
    path = _write_tmp(doc)
    try:
        writer = _Capture()

        async def run() -> None:
            manifest = build_document_manifest([(path, path.name)])
            pipeline = CorrectionPipeline(
                producer=_IdentityProducer(),
                observer=_Null(),
                output_writer=writer,
                provider_name="identity",
                model="none",
            )
            await pipeline.run(
                document_manifest=manifest,
                source_files={path.name: path},
            )

        asyncio.run(run())
        out = next(iter(writer.outputs.values()))
        src = doc.encode("utf-8")
        assert _string_contents(out) == _string_contents(src)
        assert _textline_geometry(out) == _textline_geometry(src)
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Property 3 — runs do not contaminate each other
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_runs_from_same_file_are_identical() -> None:
    """Fresh parse + fresh pipeline per run, same config ⇒ same outcome.

    Guards against state leaking through module/class-level caches. The
    planned immutable-source refactor (PLAN-1.0 P3.4) strengthens this to
    two runs over the SAME document object.
    """
    first = _outcomes(await _run_partition(_SAMPLE, None))
    second = _outcomes(await _run_partition(_SAMPLE, None))
    assert first == second
