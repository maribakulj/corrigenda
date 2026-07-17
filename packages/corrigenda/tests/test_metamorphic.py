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


# ---------------------------------------------------------------------------
# P3.2 gate — the same properties over RICH hyphenation: chains
# (PART1→BOTH→PART2), multi-page files, explicit cross-page pairs.
# ---------------------------------------------------------------------------

from corrigenda.core.schemas import HyphenRole  # noqa: E402

from tests._alto_gen import rich_alto_documents  # noqa: E402

_EXPECTED_ROLE = {
    "plain": HyphenRole.NONE,
    "part1": HyphenRole.PART1,
    "both": HyphenRole.BOTH,
    "part2": HyphenRole.PART2,
    "seam1": HyphenRole.PART1,
    "seam2": HyphenRole.PART2,
}


@settings(max_examples=30, deadline=None)
@given(doc_and_roles=rich_alto_documents())
def test_parser_recognises_every_generated_structure(
    doc_and_roles: tuple[str, dict[str, str]],
) -> None:
    """Generator↔parser cross-validation: every encoded chain member and
    seam line must surface with its intended role, and seam lines must be
    linked ACROSS the page boundary. Without this check a silent encoding
    drift would turn the downstream properties vacuous."""
    doc, expected = doc_and_roles
    path = _write_tmp(doc)
    try:
        manifest = build_document_manifest([(path, path.name)])
        by_id = {lm.line_id: lm for page in manifest.pages for lm in page.lines}
        for line_id, role in expected.items():
            lm = by_id[line_id]
            assert lm.hyphen_role == _EXPECTED_ROLE[role], (
                f"{line_id}: generated as {role!r}, parsed as {lm.hyphen_role}"
            )
            if role == "seam1":
                assert lm.hyphen_pair_page_id == "P2", (
                    "seam PART1 must link to its partner on the NEXT page"
                )
            if role == "seam2":
                assert lm.hyphen_pair_page_id == "P1", (
                    "seam PART2 must link back to the PREVIOUS page"
                )
    finally:
        path.unlink(missing_ok=True)


# All base partitions PLUS a 4-line window: over-cap chains are included
# on purpose. A 3-line chain under the 2-line cap stresses the window
# pinning's hardest case, and the invariance must STILL hold: the
# planner's over-cap cut (unlink, pinned at planner level in
# test_review_fixes.py) only runs on failure-driven descent to LINE
# granularity, and the deterministic rules producer never fails a chunk
# now that validation is identity-safe — a hard chunk failure under this
# producer is always a validator false positive, which is exactly the
# class of bug this gate exists to catch (it caught the fusion check
# firing on a source text that already contained the logical word).
_CHAIN_PARTITIONS: dict[str, ChunkPlannerConfig | None] = {
    **_PARTITIONS,
    "window-4": ChunkPlannerConfig(
        max_input_chars_per_request=200,
        max_lines_per_request=4,
        line_window_size=4,
        line_window_overlap=2,
    ),
}


@settings(max_examples=25, deadline=None)
@given(doc_and_roles=rich_alto_documents())
def test_final_text_invariant_under_chunking_with_chains(
    doc_and_roles: tuple[str, dict[str, str]],
) -> None:
    """THE P3.2 gate: chunking invariance where the reconciler actually
    works — chains, cross-page pairs, corrections landing ON hyphen
    lines (the rules substitute in every line, fragments included)."""
    doc, _ = doc_and_roles
    path = _write_tmp(doc)
    try:
        results = {
            name: _outcomes(asyncio.run(_run_partition(path, config)))
            for name, config in _CHAIN_PARTITIONS.items()
        }
        baseline = results["default"]
        assert all(
            status != LineStatus.PENDING.value for _, status in baseline.values()
        )
        for name, outcome in results.items():
            assert outcome == baseline, (
                f"partition {name!r} changed the result on a chained/"
                "cross-page document"
            )
    finally:
        path.unlink(missing_ok=True)


@settings(max_examples=25, deadline=None)
@given(doc_and_roles=rich_alto_documents())
def test_identity_producer_preserves_rich_docs(
    doc_and_roles: tuple[str, dict[str, str]],
) -> None:
    doc, _ = doc_and_roles
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
# Degenerate chain regression — identity must survive validation
# ---------------------------------------------------------------------------

# One-letter fragments: the BOTH line reads 'AA-' and the logical word of
# its forward pair IS 'AA' — the source text contains the joined word
# before the LLM says anything. Found twice by the invariance gate (once
# per Hypothesis run), fixed by making the fusion check source-relative.
_DEGENERATE_CHAIN = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"><Layout>'
    '<Page ID="P1" WIDTH="1000" HEIGHT="1000">'
    '<PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">'
    '<TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">'
    '<TextLine ID="L0" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20">'
    '<String ID="S0" CONTENT="A" HPOS="10" VPOS="10" WIDTH="80" HEIGHT="20" '
    'SUBS_TYPE="HypPart1" SUBS_CONTENT="AA"/><HYP CONTENT="-"/></TextLine>'
    '<TextLine ID="L1" HPOS="10" VPOS="40" WIDTH="900" HEIGHT="20">'
    '<String ID="S1" CONTENT="A" HPOS="10" VPOS="40" WIDTH="80" HEIGHT="20" '
    'SUBS_TYPE="HypPart2" SUBS_CONTENT="AA"/>'
    '<String ID="S2" CONTENT="A" HPOS="100" VPOS="40" WIDTH="80" HEIGHT="20" '
    'SUBS_TYPE="HypPart1" SUBS_CONTENT="AA"/><HYP CONTENT="-"/></TextLine>'
    '<TextLine ID="L2" HPOS="10" VPOS="70" WIDTH="900" HEIGHT="20">'
    '<String ID="S3" CONTENT="A" HPOS="10" VPOS="70" WIDTH="60" HEIGHT="20" '
    'SUBS_TYPE="HypPart2" SUBS_CONTENT="AA"/></TextLine>'
    '<TextLine ID="L3" HPOS="10" VPOS="100" WIDTH="900" HEIGHT="20">'
    '<String ID="S4" CONTENT="A" HPOS="10" VPOS="100" WIDTH="80" HEIGHT="20"/>'
    "</TextLine>"
    "</TextBlock></PrintSpace></Page></Layout></alto>"
)


@pytest.mark.asyncio
async def test_degenerate_chain_identity_survives_every_partition(
    tmp_path: Path,
) -> None:
    """The producer proposes the source verbatim (no e/a/o to substitute),
    so every line must come out CORRECTED in every partition. Before the
    fusion check became source-relative, this document hard-failed its
    chunk (identity rejected as fusion, three deterministic retries,
    descent budget exhausted) and the fallback blast radius depended on
    which innocent lines shared the chunk — the plain L3 fell back under
    the default partition but not under small-window."""
    path = tmp_path / "degenerate.xml"
    path.write_text(_DEGENERATE_CHAIN, encoding="utf-8")
    outcomes = {}
    for name, config in _CHAIN_PARTITIONS.items():
        doc = await _run_partition(path, config)
        for lm in (lm for page in doc.pages for lm in page.lines):
            assert lm.status is LineStatus.CORRECTED, (
                f"{name}: {lm.line_id} ended {lm.status.value} — identity "
                "was rejected by a validation false positive"
            )
        outcomes[name] = _outcomes(doc)
    assert all(o == outcomes["default"] for o in outcomes.values())
