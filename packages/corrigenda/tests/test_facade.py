"""P3.12 (§2) — the three-line happy path: load → correct → write.

``load()`` detects the format from the root namespace (one format per
document, unique basenames); ``correct()``/``correct_sync()`` run a
default pipeline with a no-op observer. Everything else — policies,
observer, explicit metadata — stays on ``CorrectionPipeline``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import corrigenda
from corrigenda.core.schemas import LineStatus
from corrigenda.errors import ParseError
from corrigenda.producers.llm_edit import LLMEditProducer
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

from tests._pipeline_harness import EXAMPLES, DictProvider

_ALTO_SAMPLE = EXAMPLES / "sample.xml"
_PAGE_SAMPLE = (
    EXAMPLES
    / "page"
    / "Descartes1637_Discours_btv1b86069594_corrected_0014_page_raw.xml"
)


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_detects_alto_by_namespace():
    document = corrigenda.load(_ALTO_SAMPLE)
    assert document.manifest.source_format == "alto"
    assert document.source_paths == {_ALTO_SAMPLE.name: _ALTO_SAMPLE}
    assert document.manifest.total_lines > 0


def test_load_detects_page_by_namespace():
    document = corrigenda.load(_PAGE_SAMPLE)
    assert document.manifest.source_format == "page"
    assert document.manifest.total_lines > 0


def test_load_accepts_string_paths():
    document = corrigenda.load(str(_ALTO_SAMPLE))
    assert document.manifest.source_format == "alto"


def test_load_refuses_a_format_mix():
    with pytest.raises(ParseError, match="one document, one format"):
        corrigenda.load(_ALTO_SAMPLE, _PAGE_SAMPLE)


def test_load_refuses_duplicate_basenames(tmp_path: Path):
    other_dir = tmp_path / "copy"
    other_dir.mkdir()
    duplicate = other_dir / _ALTO_SAMPLE.name
    duplicate.write_bytes(_ALTO_SAMPLE.read_bytes())
    with pytest.raises(ParseError, match="basename"):
        corrigenda.load(_ALTO_SAMPLE, duplicate)


def test_load_refuses_unknown_namespace(tmp_path: Path):
    p = tmp_path / "other.xml"
    p.write_text('<root xmlns="urn:not-a-transcription"/>', encoding="utf-8")
    with pytest.raises(ParseError, match="neither ALTO nor PAGE"):
        corrigenda.load(p)


def test_load_needs_at_least_one_path():
    with pytest.raises(ParseError, match="at least one"):
        corrigenda.load()


# ---------------------------------------------------------------------------
# correct_sync() / correct() — the full three lines
# ---------------------------------------------------------------------------


def test_three_lines_rules_producer(tmp_path: Path):
    document = corrigenda.load(_ALTO_SAMPLE)
    result = corrigenda.correct_sync(
        document,
        producer=RulesProducer([SubstitutionRule("e", "3", name="demo")]),
    )
    result.write(tmp_path / "out")

    assert (tmp_path / "out" / _ALTO_SAMPLE.name).exists()
    assert (tmp_path / "out" / "report.json").exists()
    # Provenance flows from the producer's own declaration.
    assert result.report.provenance is not None
    assert result.report.provenance.producer.name == "rules"
    # Input untouched (ADR-011).
    assert all(
        lm.status is LineStatus.PENDING
        for page in document.manifest.pages
        for lm in page.lines
    )


def test_three_lines_llm_producer_async():
    import asyncio

    async def main():
        document = corrigenda.load(_ALTO_SAMPLE)
        producer = LLMEditProducer(DictProvider({}), api_key="k", model="m")
        return await corrigenda.correct(document, producer=producer)

    result = asyncio.run(main())
    assert result.report.total_lines > 0
    # Identity run: every line decided, none corrected away from source.
    assert all(d.final_text == d.source_text for d in result.decisions.decisions)


def test_correct_sync_on_page_document(tmp_path: Path):
    document = corrigenda.load(_PAGE_SAMPLE)
    result = corrigenda.correct_sync(
        document,
        producer=RulesProducer([]),  # no rules: pure identity
    )
    assert result.report.provenance is not None
    assert result.report.provenance.source_format == "page"
    assert _PAGE_SAMPLE.name in result.corrected_files
