"""Plan V4.2 phase 3 — invariants over the EXTERNAL Gallica corpus.

The unit and property suites share a blind spot: the same person wrote
the code and the generators, so both encode the same assumptions. This
suite runs the real pipeline over ALTO files produced by a REAL OCR
pipeline (Gallica/BnF) on documents never opened during development —
see external_corpus/manifest.json for the pinned set.

The corpus comes in two tiers:

- **fetched** — downloaded at CI time by ``external_corpus/fetch.py``
  (a dedicated NON-BLOCKING job — network flakiness must not gate
  merges while the corpus job builds its track record). Locally:

      python tests/external_corpus/fetch.py && pytest -m external_corpus

- **pinned** — real Gallica pages committed under
  ``external_corpus/pinned/`` (see its README for provenance rules).
  These run in the DEFAULT test suite, offline, and BLOCK merges: no
  ``external_corpus`` marker, no self-skip.

When neither tier has files, the tests self-skip and the default
``pytest`` run is unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from corrigenda.core.schemas import HyphenRole, LineStatus
from corrigenda.errors import CorrectionError
from corrigenda.formats.alto.parser import build_document_manifest

from tests._pipeline_harness import DictProvider, RecordingObserver, _NoopWriter
from corrigenda.core.pipeline import CorrectionPipeline

_CACHE = Path(
    os.environ.get(
        "CORRIGENDA_EXTERNAL_CORPUS_DIR",
        Path(__file__).parent / "external_corpus" / ".cache",
    )
)
_PINNED_DIR = Path(__file__).parent / "external_corpus" / "pinned"
_FETCHED = sorted(_CACHE.glob("*.alto.xml")) if _CACHE.is_dir() else []
_PINNED = sorted(_PINNED_DIR.glob("*.alto.xml")) if _PINNED_DIR.is_dir() else []


def _corpus_params() -> list:
    """Pinned files run everywhere (blocking); fetched files only under
    ``-m external_corpus``. An empty corpus yields one skipped param so
    collection never errors."""
    params = [pytest.param(p, id=f"pinned-{p.name}") for p in _PINNED] + [
        pytest.param(p, id=p.name, marks=pytest.mark.external_corpus) for p in _FETCHED
    ]
    if not params:
        params = [
            pytest.param(
                None,
                id="corpus-absent",
                marks=pytest.mark.skip(
                    reason="no pinned corpus committed and external corpus "
                    "not fetched (run tests/external_corpus/fetch.py)"
                ),
            )
        ]
    return params


@pytest.mark.parametrize("xml_path", _corpus_params(), ids=None)
def test_parses_or_fails_classified(xml_path: Path) -> None:
    """§8.4 at the front door, on real-world OCR output."""
    try:
        doc = build_document_manifest([(xml_path, xml_path.name)])
    except CorrectionError:
        return  # classified — acceptable for a hostile real-world file
    assert doc.total_lines == sum(len(p.lines) for p in doc.pages)


@pytest.mark.parametrize("xml_path", _corpus_params(), ids=None)
def test_identity_run_preserves_invariants(xml_path: Path) -> None:
    """Identity pipeline run over a real file: geometry untouched, no
    mixed hyphen pairs, every line in a terminal state."""
    try:
        doc = build_document_manifest([(xml_path, xml_path.name)])
    except CorrectionError:
        pytest.skip("file rejected at parse (classified) — nothing to run")
    if doc.total_lines == 0:
        pytest.skip("no text lines on this page")

    geometry_before = {
        (lm.page_id, lm.line_id): (
            lm.coords.hpos,
            lm.coords.vpos,
            lm.coords.width,
            lm.coords.height,
        )
        for page in doc.pages
        for lm in page.lines
    }

    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),  # identity: every line echoed unchanged
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        output_writer=_NoopWriter(),
    )
    result = pipeline.run_sync(
        document_manifest=doc,
        source_files={xml_path.name: xml_path},
        apply=False,
    )

    # Geometry is never touched by a run.
    geometry_after = {
        (lm.page_id, lm.line_id): (
            lm.coords.hpos,
            lm.coords.vpos,
            lm.coords.width,
            lm.coords.height,
        )
        for page in doc.pages
        for lm in page.lines
    }
    assert geometry_after == geometry_before

    lines_by_key = {
        (lm.page_id, lm.line_id): lm for page in doc.pages for lm in page.lines
    }
    for lm in lines_by_key.values():
        # Every line reached a terminal state.
        assert lm.status in (
            LineStatus.CORRECTED,
            LineStatus.FALLBACK,
        ), f"{lm.line_id}: non-terminal status {lm.status}"
        # No mixed hyphen pair: PART1 corrected ⇔ PART2 corrected.
        if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
            partner = lines_by_key.get(
                (lm.hyphen_pair_page_id or lm.page_id, lm.hyphen_pair_line_id)
            )
            if partner is not None:
                assert (lm.corrected_text is not None) == (
                    partner.corrected_text is not None
                ), f"mixed pair {lm.line_id}/{partner.line_id}"

    assert result.report.total_lines == doc.total_lines
