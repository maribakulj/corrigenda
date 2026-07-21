"""P3.8 / ADR-012 — LossPolicy: REPORT attributes, STRICT rejects.

The PAGE rewriter drops a line's ``Word`` geometry when a correction
changes the word count (6.2 P4 slow path). Under REPORT (default) the
loss projects, is counted run-wide (``format_losses``) and attributed
per line (``ProjectionStage.losses``); under STRICT the whole hyphen
unit falls back to source text BEFORE any output exists, so the source
markup keeps its ``Word`` geometry.
"""

from __future__ import annotations

from pathlib import Path

from corrigenda import CorrectionPipeline
from corrigenda.core.identity import LineRef, line_ref
from corrigenda.core.schemas import (
    HyphenRole,
    LineManifest,
    LineStatus,
    LineTrace,
    LossPolicy,
)
from corrigenda.formats.page.parser import build_document_manifest
from corrigenda.formats.page.rewriter import rewrite_page_file

from tests._pipeline_harness import DictProvider, RecordingObserver

_WORDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 300,0 300,80 0,80"/>
      <TextLine id="ln1"><Coords points="0,0 300,0 300,20 0,20"/>
        <Word id="w1"><Coords points="0,0 90,0 90,20 0,20"/>
          <TextEquiv><Unicode>helo</Unicode></TextEquiv></Word>
        <Word id="w2"><Coords points="100,0 200,0 200,20 100,20"/>
          <TextEquiv><Unicode>wrld</Unicode></TextEquiv></Word>
        <TextEquiv><Unicode>helo wrld</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="ln2"><Coords points="0,30 300,30 300,50 0,50"/>
        <TextEquiv><Unicode>sans mots</Unicode></TextEquiv></TextLine>
    </TextRegion>
  </Page>
</PcGts>
"""


def _write(tmp_path: Path, xml: str, name: str = "f.xml") -> Path:
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return p


def _run(path: Path, corrections: dict[str, str], **pipeline_kwargs):
    doc = build_document_manifest([(path, path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider(corrections),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        **pipeline_kwargs,
    )
    return pipeline.run_sync(document_manifest=doc, source_files={path.name: path})


# ---------------------------------------------------------------------------
# Parser — word_count reaches the manifest
# ---------------------------------------------------------------------------


def test_page_parser_records_word_count(tmp_path: Path):
    doc = build_document_manifest([(p := _write(tmp_path, _WORDS_XML), p.name)])
    ln1, ln2 = doc.pages[0].lines
    assert ln1.word_count == 2  # two Word children
    assert ln2.word_count is None  # no word markup → nothing to lose


# ---------------------------------------------------------------------------
# REPORT (default) — the loss projects, is counted AND attributed
# ---------------------------------------------------------------------------


def test_report_mode_attributes_losses_per_line(tmp_path: Path):
    p = _write(tmp_path, _WORDS_XML)
    result = _run(p, {"ln1": "helloworld"})  # 2 words → 1: slow path

    by_ref = result.decisions.by_ref
    d = by_ref[LineRef(page_id="p.png", line_id="ln1")]
    assert d.status is LineStatus.CORRECTED
    assert d.final_text == "helloworld"

    # Run-wide aggregate (pre-existing surface).
    assert result.report.format_losses is not None
    assert result.report.format_losses["words_dropped"] == 2

    # ADR-012 — per-decision attribution on the projection stage.
    outcome = next(o for o in result.report.lines if o.line_id == "ln1")
    assert outcome.projection is not None
    assert outcome.projection.rewriter_path == "slow_path"
    assert outcome.projection.losses is not None
    assert outcome.projection.losses["words_dropped"] == 2

    untouched = next(o for o in result.report.lines if o.line_id == "ln2")
    assert untouched.projection is not None
    assert untouched.projection.losses is None  # lost nothing

    # The artefact really dropped the Word geometry.
    assert b"<Word" not in result.corrected_files[p.name]


def test_rewrite_result_attribution_sums_to_aggregate(tmp_path: Path):
    p = _write(tmp_path, _WORDS_XML)
    doc = build_document_manifest([(p, p.name)])
    doc.pages[0].lines[0].corrected_text = "helloworld"

    result = rewrite_page_file(p, doc.pages, "prov", "mdl")
    assert set(result.losses_by_line) == {"ln1"}
    summed: dict[str, int] = {}
    for line_losses in result.losses_by_line.values():
        for key, count in line_losses.items():
            summed[key] = summed.get(key, 0) + count
    assert summed == result.losses


# ---------------------------------------------------------------------------
# STRICT — the unit falls back, the source keeps its Word geometry
# ---------------------------------------------------------------------------


def test_strict_rejects_word_count_changing_correction(tmp_path: Path):
    p = _write(tmp_path, _WORDS_XML)
    result = _run(p, {"ln1": "helloworld"}, loss_policy=LossPolicy(strict=True))

    d = result.decisions.by_ref[LineRef(page_id="p.png", line_id="ln1")]
    assert d.status is LineStatus.FALLBACK
    assert d.final_text == "helo wrld"  # source text, not the correction
    assert d.fallback_reason is not None
    assert d.fallback_reason.startswith("format_loss")
    # Reason code aggregates like every other fallback family.
    assert "format_loss" in result.fallback_reasons

    # No projection loss: the rewrite saw an untouched line.
    assert b"<Word" in result.corrected_files[p.name]
    losses = result.report.format_losses or {}
    assert "words_dropped" not in losses


def test_strict_accepts_word_count_preserving_correction(tmp_path: Path):
    p = _write(tmp_path, _WORDS_XML)
    result = _run(p, {"ln1": "hello world"}, loss_policy=LossPolicy(strict=True))

    d = result.decisions.by_ref[LineRef(page_id="p.png", line_id="ln1")]
    assert d.status is LineStatus.CORRECTED
    assert d.final_text == "hello world"
    # Fast path: Word geometry survives, updated in place.
    assert b"<Word" in result.corrected_files[p.name]


def test_strict_ignores_lines_without_word_markup(tmp_path: Path):
    p = _write(tmp_path, _WORDS_XML)
    # ln2 has no Word children: any word count projects losslessly.
    result = _run(p, {"ln2": "sansmots"}, loss_policy=LossPolicy(strict=True))
    d = result.decisions.by_ref[LineRef(page_id="p.png", line_id="ln2")]
    assert d.status is LineStatus.CORRECTED
    assert d.final_text == "sansmots"


# ---------------------------------------------------------------------------
# STRICT — unit atomicity (ADR-010): a flagged member pulls its partner
# ---------------------------------------------------------------------------


def _hyphen_pair_lines() -> list[LineManifest]:
    from corrigenda.core.schemas import Coords

    part1 = LineManifest(
        line_id="l1",
        page_id="pg",
        block_id="b",
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=0, vpos=0, width=100, height=10),
        ocr_text="cor-",
        word_count=2,
        corrected_text="corrigé en trois mots",  # 2 → 4: unprojectable
        status=LineStatus.CORRECTED,
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="l2",
        hyphen_pair_page_id="pg",
    )
    part2 = LineManifest(
        line_id="l2",
        page_id="pg",
        block_id="b",
        line_order_global=1,
        line_order_in_block=1,
        coords=Coords(hpos=0, vpos=20, width=100, height=10),
        ocr_text="rigé",
        corrected_text="rigé",
        status=LineStatus.CORRECTED,
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="l1",
        hyphen_pair_page_id="pg",
    )
    return [part1, part2]


def test_strict_pass_pulls_the_whole_hyphen_unit():
    from corrigenda.core.schemas import DocumentManifest, PageManifest

    lines = _hyphen_pair_lines()
    page = PageManifest(
        page_id="pg",
        page_index=0,
        source_file="f.xml",
        page_width=100,
        page_height=100,
        blocks=[],
        lines=lines,
    )
    doc = DocumentManifest(
        document_id="d", source_files=["f.xml"], pages=[page], total_lines=2
    )

    class _Noop:
        wants_geometry = False
        wants_image = False

        async def produce(self, payload, *, options):  # pragma: no cover
            raise NotImplementedError

        def on_event(self, event_type, payload):
            pass

    noop = _Noop()
    pipeline = CorrectionPipeline(
        producer=noop, observer=noop, loss_policy=LossPolicy(strict=True)
    )
    all_lines = {line_ref(lm): lm for lm in lines}
    traces = {
        line_ref(lm): LineTrace(
            line_id=lm.line_id, page_id=lm.page_id, source_ocr_text=lm.ocr_text
        )
        for lm in lines
    }
    pipeline._loss_policy_pass(
        document_manifest=doc, all_lines=all_lines, traces=traces
    )

    part1, part2 = lines
    assert part1.status is LineStatus.FALLBACK
    assert part1.corrected_text == part1.ocr_text
    assert part2.status is LineStatus.FALLBACK, "partner must fall with the unit"
    assert part2.corrected_text == part2.ocr_text
    r1 = traces[line_ref(part1)].fallback_reason
    r2 = traces[line_ref(part2)].fallback_reason
    assert r1 is not None and r1.startswith("format_loss:")
    assert r2 == "format_loss_pair_atomicity"


# ---------------------------------------------------------------------------
# Provenance — LossPolicy is part of the §11 fingerprint
# ---------------------------------------------------------------------------


def test_strict_changes_the_config_fingerprint(tmp_path: Path):
    class _Noop:
        wants_geometry = False
        wants_image = False

        async def produce(self, payload, *, options):  # pragma: no cover
            raise NotImplementedError

        def on_event(self, event_type, payload):
            pass

    noop = _Noop()
    default = CorrectionPipeline(producer=noop, observer=noop)
    strict = CorrectionPipeline(
        producer=noop, observer=noop, loss_policy=LossPolicy(strict=True)
    )
    assert default.config_fingerprint() != strict.config_fingerprint()
