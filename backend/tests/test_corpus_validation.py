"""Sprint 3 — Corpus validation tests with metrics.

End-to-end tests that parse ALTO fixtures, simulate LLM corrections,
run reconciliation + rewriting, and verify invariants + collect metrics.

Covers:
  - Explicit hyphen pairs: néces-/saires, con-/damne, fonda-/mentaux
  - Heuristic hyphen pairs: pratica-/bles
  - Coherent correction, fusion, migration, boundary divergence
  - Unchanged lines, fast path, slow path
  - Rewriter path distribution metrics
  - No incoherent pair survives reconciliation
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from app.alto.hyphenation import (
    ReconcileMetrics,
    classify_reconcile_outcome,
    reconcile_hyphen_pair,
)
from app.alto.parser import parse_alto_file
from app.alto.rewriter import RewriterMetrics, rewrite_alto_file
from app.schemas import HyphenRole

NS = "http://www.loc.gov/standards/alto/ns-v3#"


def _ns(local: str) -> str:
    return f"{{{NS}}}{local}"


# ---------------------------------------------------------------------------
# ALTO fixture builder
# ---------------------------------------------------------------------------


def _alto_xml(textlines_xml: str, page_id: str = "P1", block_id: str = "TB1") -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="{NS}">
  <Description>
    <MeasurementUnit>pixel</MeasurementUnit>
    <OCRProcessing ID="OCR_1">
      <ocrProcessingStep/>
    </OCRProcessing>
    <Processing/>
  </Description>
  <Layout>
    <Page ID="{page_id}" WIDTH="2480" HEIGHT="3508">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="2480" HEIGHT="3508">
        <TextBlock ID="{block_id}" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="400">
{textlines_xml}
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def _write_fixture(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Helpers for end-to-end simulation
# ---------------------------------------------------------------------------


def _simulate_corrections(
    pages,
    corrections: dict[str, str | None],
) -> None:
    """Apply simulated LLM corrections to parsed pages."""
    for page in pages:
        for lm in page.lines:
            if lm.line_id in corrections:
                lm.corrected_text = corrections[lm.line_id]


def _reconcile_all_pairs(pages) -> ReconcileMetrics:
    """Run reconcile_hyphen_pair on all PART1 lines, collecting metrics."""
    metrics = ReconcileMetrics()
    for page in pages:
        line_by_id = {lm.line_id: lm for lm in page.lines}
        for lm in page.lines:
            if lm.hyphen_role != HyphenRole.PART1 or not lm.hyphen_pair_line_id:
                continue
            part2 = line_by_id.get(lm.hyphen_pair_line_id)
            if part2 is None:
                continue
            corrected_p1 = lm.corrected_text or lm.ocr_text
            corrected_p2 = part2.corrected_text or part2.ocr_text

            final_p1, final_p2, subs = reconcile_hyphen_pair(
                lm,
                part2,
                corrected_p1,
                corrected_p2,
            )
            lm.corrected_text = final_p1
            lm.hyphen_subs_content = subs
            part2.corrected_text = final_p2
            part2.hyphen_subs_content = subs

            outcome = classify_reconcile_outcome(
                lm.ocr_text,
                part2.ocr_text,
                corrected_p1,
                corrected_p2,
                final_p1,
                final_p2,
                subs,
            )
            if outcome == "coherent":
                metrics.coherent += 1
            elif outcome == "fallback":
                metrics.fallback += 1
            else:
                metrics.neutralised += 1
    return metrics


def _rewrite_and_parse(
    xml_path: Path,
    pages,
    tmp_path: Path,
) -> tuple[etree._Element, RewriterMetrics]:
    """Rewrite and return (root_element, metrics)."""
    xml_bytes, metrics, _paths = rewrite_alto_file(xml_path, pages, "test", "test-model")
    out = tmp_path / "out.xml"
    out.write_bytes(xml_bytes)
    return etree.fromstring(xml_bytes), metrics


def _get_subs(root, line_id: str, position: str = "last") -> tuple[str | None, str | None]:
    """Extract SUBS_TYPE/SUBS_CONTENT from a String in a TextLine."""
    tl = root.find(f".//{_ns('TextLine')}[@ID='{line_id}']")
    if tl is None:
        return None, None
    strings = [c for c in tl if c.tag == _ns("String")]
    if not strings:
        return None, None
    target = strings[-1] if position == "last" else strings[0]
    return target.get("SUBS_TYPE"), target.get("SUBS_CONTENT")


# ===========================================================================
# FIXTURE: Explicit hyphen pair — néces-/saires → nécessaires
# ===========================================================================

NECES_SAIRES_XML = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="Il" HPOS="100" VPOS="100" WIDTH="60" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="néces" HPOS="180" VPOS="100" WIDTH="200" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="nécessaires"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S3" CONTENT="saires" HPOS="100" VPOS="180" WIDTH="180" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="nécessaires"/>
            <SP WIDTH="20"/>
            <String ID="S4" CONTENT="pour" HPOS="310" VPOS="180" WIDTH="120" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S5" CONTENT="vivre." HPOS="450" VPOS="180" WIDTH="160" HEIGHT="50"/>
          </TextLine>""")


class TestNecessaires:
    """Explicit hyphen pair: néces-/saires (subs=nécessaires)."""

    def test_coherent_correction(self, tmp_path):
        """LLM corrects OCR errors but keeps hyphen structure → coherent."""
        xml_path = _write_fixture(tmp_path, "neces.xml", NECES_SAIRES_XML)
        pages, _ = parse_alto_file(xml_path, "neces.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "Il néces-",
                "TL2": "saires pour vivre.",
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.coherent == 1
        assert rec.fallback == 0

        root, rw = _rewrite_and_parse(xml_path, pages, tmp_path)
        # SUBS preserved
        st, sc = _get_subs(root, "TL1", "last")
        assert st == "HypPart1"
        assert sc == "nécessaires"

    def test_fusion_detected(self, tmp_path):
        """LLM fuses néces- + saires → nécessaires on PART1 → fallback."""
        xml_path = _write_fixture(tmp_path, "neces.xml", NECES_SAIRES_XML)
        pages, _ = parse_alto_file(xml_path, "neces.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "Il nécessaires",  # fusion: full word on PART1
                "TL2": "pour vivre.",  # PART2 lost its first word
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.fallback == 1
        assert rec.coherent == 0

        # Both sides reverted to OCR
        for page in pages:
            for lm in page.lines:
                if lm.line_id == "TL1":
                    assert lm.corrected_text == "Il néces-"
                if lm.line_id == "TL2":
                    assert lm.corrected_text == "saires pour vivre."

    def test_migration_detected(self, tmp_path):
        """LLM pulled PART2 content into PART1 → fallback both."""
        xml_path = _write_fixture(tmp_path, "neces.xml", NECES_SAIRES_XML)
        pages, _ = parse_alto_file(xml_path, "neces.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "Il néces- saires pour vivre.",  # absorbed PART2
                "TL2": "",
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.fallback == 1


# ===========================================================================
# FIXTURE: Explicit hyphen pair — con-/damne → condamne
# ===========================================================================

CONDAMNE_XML = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="On" HPOS="100" VPOS="100" WIDTH="60" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="con" HPOS="180" VPOS="100" WIDTH="120" HEIGHT="50"
                    SUBS_TYPE="HypPart1" SUBS_CONTENT="condamne"/>
            <HYP CONTENT="-"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S3" CONTENT="damne" HPOS="100" VPOS="180" WIDTH="160" HEIGHT="50"
                    SUBS_TYPE="HypPart2" SUBS_CONTENT="condamne"/>
            <SP WIDTH="20"/>
            <String ID="S4" CONTENT="le" HPOS="280" VPOS="180" WIDTH="50" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S5" CONTENT="tyran." HPOS="350" VPOS="180" WIDTH="160" HEIGHT="50"/>
          </TextLine>""")


class TestCondamne:
    """Explicit hyphen pair: con-/damne (subs=condamne)."""

    def test_coherent(self, tmp_path):
        xml_path = _write_fixture(tmp_path, "cond.xml", CONDAMNE_XML)
        pages, _ = parse_alto_file(xml_path, "cond.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "On con-",
                "TL2": "damne le tyran.",
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.coherent == 1

        root, rw = _rewrite_and_parse(xml_path, pages, tmp_path)
        st, sc = _get_subs(root, "TL1", "last")
        assert st == "HypPart1"
        assert sc == "condamne"

    def test_subs_mismatch_fallback(self, tmp_path):
        """LLM changed boundary word so join ≠ subs → fallback."""
        xml_path = _write_fixture(tmp_path, "cond.xml", CONDAMNE_XML)
        pages, _ = parse_alto_file(xml_path, "cond.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "On con-",
                "TL2": "tinue le tyran.",  # con+tinue ≠ condamne
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.fallback == 1


# ===========================================================================
# FIXTURE: Heuristic hyphen pair — pratica-/bles (no SUBS_TYPE)
# ===========================================================================

PRATICABLES_XML = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="Les" HPOS="100" VPOS="100" WIDTH="80" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="routes" HPOS="200" VPOS="100" WIDTH="180" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S3" CONTENT="pratica-" HPOS="400" VPOS="100" WIDTH="240" HEIGHT="50"/>
          </TextLine>
          <TextLine ID="TL2" HPOS="100" VPOS="180" WIDTH="2000" HEIGHT="60">
            <String ID="S4" CONTENT="bles" HPOS="100" VPOS="180" WIDTH="110" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S5" CONTENT="sont" HPOS="230" VPOS="180" WIDTH="120" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S6" CONTENT="rares." HPOS="370" VPOS="180" WIDTH="160" HEIGHT="50"/>
          </TextLine>""")


class TestPraticables:
    """Heuristic hyphen pair: pratica-/bles (no SUBS in XML)."""

    def test_heuristic_coherent(self, tmp_path):
        """Correction keeps structure → neutralised (no subs reconstructed)."""
        xml_path = _write_fixture(tmp_path, "prat.xml", PRATICABLES_XML)
        pages, _ = parse_alto_file(xml_path, "prat.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "Les routes pratica-",
                "TL2": "bles sont rares.",
            },
        )
        rec = _reconcile_all_pairs(pages)
        # Heuristic → subs_content=None → neutralised
        assert rec.neutralised == 1
        assert rec.coherent == 0

        root, rw = _rewrite_and_parse(xml_path, pages, tmp_path)
        # No SUBS invented on heuristic pairs
        st, sc = _get_subs(root, "TL1", "last")
        assert st is None
        assert sc is None

    def test_heuristic_boundary_diverged(self, tmp_path):
        """LLM changed PART2 first word completely → fallback."""
        xml_path = _write_fixture(tmp_path, "prat.xml", PRATICABLES_XML)
        pages, _ = parse_alto_file(xml_path, "prat.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "Les routes pratica-",
                "TL2": "urgentes sont rares.",  # "bles" → "urgentes" = diverged
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.fallback == 1

    def test_heuristic_lost_dash(self, tmp_path):
        """LLM removed trailing dash → fallback."""
        xml_path = _write_fixture(tmp_path, "prat.xml", PRATICABLES_XML)
        pages, _ = parse_alto_file(xml_path, "prat.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "Les routes praticables",  # no dash
                "TL2": "sont rares.",
            },
        )
        rec = _reconcile_all_pairs(pages)
        assert rec.fallback == 1


# ===========================================================================
# FIXTURE: sample.xml (real corpus) — end-to-end rewriter metrics
# ===========================================================================

SAMPLE_XML_PATH = Path(__file__).resolve().parent.parent.parent / "examples" / "sample.xml"


@pytest.mark.skipif(not SAMPLE_XML_PATH.exists(), reason="sample.xml not found")
class TestSampleCorpus:
    """End-to-end tests on the sample.xml fixture."""

    def test_parse_structure(self):
        """Verify parser extracts expected structure from sample.xml."""
        pages, _ = parse_alto_file(SAMPLE_XML_PATH, "sample.xml")
        assert len(pages) == 2
        total_lines = sum(len(p.lines) for p in pages)
        assert total_lines == 10

        # Explicit pairs
        explicit_p1 = [
            lm
            for p in pages
            for lm in p.lines
            if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_source_explicit
        ]
        assert len(explicit_p1) == 2  # TL4 (dénon-) and TL8 (fonda-)

        # Heuristic pair
        heuristic_p1 = [
            lm
            for p in pages
            for lm in p.lines
            if lm.hyphen_role == HyphenRole.PART1 and not lm.hyphen_source_explicit
        ]
        assert len(heuristic_p1) == 1  # TL6 (bouleYerse-)

    def test_unchanged_rewrite_metrics(self, tmp_path):
        """No corrections → all lines untouched."""
        pages, _ = parse_alto_file(SAMPLE_XML_PATH, "sample.xml")
        # No corrections applied
        root, metrics = _rewrite_and_parse(SAMPLE_XML_PATH, pages, tmp_path)
        assert metrics.untouched == 10
        assert metrics.fast_path == 0
        assert metrics.slow_path == 0
        assert metrics.subs_only == 0

    def test_corrected_rewrite_metrics(self, tmp_path):
        """Apply corrections to all lines → verify path distribution."""
        pages, _ = parse_alto_file(SAMPLE_XML_PATH, "sample.xml")

        # Corrections that preserve word count (fast path)
        fast_path_corrections = {
            "TL1": "HISTOIRE DE LA RÉVOLUTION",  # fix RÉVOLUTIOM
            "TL2": "La France traversa une période troublée.",  # fix Frauce, uue, tronblée
            "TL3": "Les citoyens se soulevèrent contre l'oppression.",  # fix citoyeus etc
            "TL5": "çait les abus du pouvoir absolu.",  # fix pouvolr
            "TL8": "L'assemblée nationale proclama les droits fonda-",  # fix uationale
            "TL9": "mentaux de l'homme et du citoyen.",  # fix l'hoinme, citoyeu
            "TL10": "Ces principes allaient transformer le monde entier.",  # fix priucipes etc
        }
        # Corrections for hyphen lines (keep structure)
        hyphen_corrections = {
            "TL4": "Le peuple réclamait la liberté et dénon-",  # same words
            "TL6": "Chaque journée apportait son lot de bouleverse-",  # fix jouruée, sou, bouleYerse
            "TL7": "ments.",  # unchanged
        }
        all_corrections = {**fast_path_corrections, **hyphen_corrections}
        _simulate_corrections(pages, all_corrections)

        rec = _reconcile_all_pairs(pages)
        assert rec.total == 3  # 3 hyphen pairs

        root, rw_metrics = _rewrite_and_parse(SAMPLE_XML_PATH, pages, tmp_path)
        assert rw_metrics.total_lines == 10
        # At least some lines should be fast path (word count preserved)
        assert rw_metrics.fast_path > 0
        # Slow path should be minimal or zero with careful corrections
        # Untouched should be any lines where text didn't change
        assert (
            rw_metrics.untouched
            + rw_metrics.fast_path
            + rw_metrics.slow_path
            + rw_metrics.subs_only
            == 10
        )


# ===========================================================================
# Invariant: no incoherent pair survives
# ===========================================================================


class TestIncoherentPairInvariant:
    """
    Core invariant: after reconciliation, every hyphen pair is either
    fully corrected (both sides) or fully fallen back (both sides OCR).
    No mixed pair (one corrected, one OCR) can exist.
    """

    def test_no_mixed_pair_after_reconcile(self, tmp_path):
        """Simulate a case where LLM gives incoherent pair → verify both fall back."""
        xml_path = _write_fixture(tmp_path, "neces.xml", NECES_SAIRES_XML)
        pages, _ = parse_alto_file(xml_path, "neces.xml")

        # Simulate: PART1 corrected, PART2 has different boundary word
        _simulate_corrections(
            pages,
            {
                "TL1": "Il néces-",
                "TL2": "urgentes pour vivre.",  # boundary diverged from "saires"
            },
        )
        _reconcile_all_pairs(pages)

        # Verify: both sides must be OCR (fallback)
        line_by_id = {lm.line_id: lm for p in pages for lm in p.lines}
        p1 = line_by_id["TL1"]
        p2 = line_by_id["TL2"]
        # Both should be at OCR source
        assert p1.corrected_text == p1.ocr_text
        assert p2.corrected_text == p2.ocr_text

    def test_no_stale_subs_after_fallback(self, tmp_path):
        """After fallback, subs_content is neutralised (None)."""
        xml_path = _write_fixture(tmp_path, "cond.xml", CONDAMNE_XML)
        pages, _ = parse_alto_file(xml_path, "cond.xml")

        _simulate_corrections(
            pages,
            {
                "TL1": "On condamne",  # fusion: full word, no dash
                "TL2": "le tyran.",
            },
        )
        _reconcile_all_pairs(pages)

        line_by_id = {lm.line_id: lm for p in pages for lm in p.lines}
        assert line_by_id["TL1"].hyphen_subs_content is None
        assert line_by_id["TL2"].hyphen_subs_content is None


# ===========================================================================
# Rewriter path tests: unchanged line stays XML-identical
# ===========================================================================


class TestUnchangedLineIdentity:
    """Lines with no correction must be byte-identical in output."""

    def test_unchanged_line_xml_identical(self, tmp_path):
        xml_content = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="Bonjour" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="50" WC="0.95" CC="1234567"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="monde" HPOS="320" VPOS="100" WIDTH="160" HEIGHT="50" WC="0.92"/>
          </TextLine>""")
        xml_path = _write_fixture(tmp_path, "unch.xml", xml_content)
        pages, _ = parse_alto_file(xml_path, "unch.xml")
        # No corrections
        root, metrics = _rewrite_and_parse(xml_path, pages, tmp_path)
        assert metrics.untouched == 1
        assert metrics.fast_path == 0
        assert metrics.slow_path == 0

        # Verify attributes preserved
        tl = root.find(f".//{_ns('TextLine')}[@ID='TL1']")
        strings = [c for c in tl if c.tag == _ns("String")]
        assert strings[0].get("WC") == "0.95"
        assert strings[0].get("CC") == "1234567"


# ===========================================================================
# Rewriter path: fast path preserves attributes
# ===========================================================================


class TestFastPathPreservation:
    """Fast path (word count same) only changes CONTENT, preserves all else."""

    def test_fast_path_attributes(self, tmp_path):
        xml_content = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="Bonjoar" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="50" WC="0.70" STYLEREFS="font1"/>
            <SP WIDTH="20" HPOS="300" VPOS="100"/>
            <String ID="S2" CONTENT="moude" HPOS="320" VPOS="100" WIDTH="160" HEIGHT="50" WC="0.65"/>
          </TextLine>""")
        xml_path = _write_fixture(tmp_path, "fast.xml", xml_content)
        pages, _ = parse_alto_file(xml_path, "fast.xml")
        _simulate_corrections(pages, {"TL1": "Bonjour monde"})

        root, metrics = _rewrite_and_parse(xml_path, pages, tmp_path)
        assert metrics.fast_path == 1

        tl = root.find(f".//{_ns('TextLine')}[@ID='TL1']")
        strings = [c for c in tl if c.tag == _ns("String")]
        assert strings[0].get("CONTENT") == "Bonjour"
        assert strings[0].get("WC") == "0.70"
        assert strings[0].get("STYLEREFS") == "font1"
        assert strings[0].get("ID") == "S1"
        assert strings[1].get("CONTENT") == "monde"

        # SP preserved
        sps = [c for c in tl if c.tag == _ns("SP")]
        assert len(sps) == 1
        assert sps[0].get("WIDTH") == "20"


# ===========================================================================
# Rewriter path: slow path observable
# ===========================================================================


class TestSlowPathObservable:
    """Slow path triggered when word count changes — metric is observable."""

    def test_slow_path_word_count_change(self, tmp_path):
        xml_content = _alto_xml("""\
          <TextLine ID="TL1" HPOS="100" VPOS="100" WIDTH="2000" HEIGHT="60">
            <String ID="S1" CONTENT="Bonjour" HPOS="100" VPOS="100" WIDTH="200" HEIGHT="50"/>
            <SP WIDTH="20"/>
            <String ID="S2" CONTENT="monde" HPOS="320" VPOS="100" WIDTH="160" HEIGHT="50"/>
          </TextLine>""")
        xml_path = _write_fixture(tmp_path, "slow.xml", xml_content)
        pages, _ = parse_alto_file(xml_path, "slow.xml")
        _simulate_corrections(pages, {"TL1": "Bonjour le monde"})  # 2→3 words

        root, metrics = _rewrite_and_parse(xml_path, pages, tmp_path)
        assert metrics.slow_path == 1
        assert metrics.fast_path == 0

        tl = root.find(f".//{_ns('TextLine')}[@ID='TL1']")
        strings = [c for c in tl if c.tag == _ns("String")]
        assert len(strings) == 3
        assert strings[0].get("CONTENT") == "Bonjour"
        assert strings[1].get("CONTENT") == "le"
        assert strings[2].get("CONTENT") == "monde"


# ===========================================================================
# Diagnostic report: aggregate metrics from sample.xml
# ===========================================================================


@pytest.mark.skipif(not SAMPLE_XML_PATH.exists(), reason="sample.xml not found")
def test_diagnostic_report(tmp_path, capsys):
    """Print a diagnostic report of metrics for the sample corpus."""
    pages, _ = parse_alto_file(SAMPLE_XML_PATH, "sample.xml")

    corrections = {
        "TL1": "HISTOIRE DE LA RÉVOLUTION",
        "TL2": "La France traversa une période troublée.",
        "TL3": "Les citoyens se soulevèrent contre l'oppression.",
        "TL4": "Le peuple réclamait la liberté et dénon-",
        "TL5": "çait les abus du pouvoir absolu.",
        "TL6": "Chaque journée apportait son lot de bouleverse-",
        "TL7": "ments.",
        "TL8": "L'assemblée nationale proclama les droits fonda-",
        "TL9": "mentaux de l'homme et du citoyen.",
        "TL10": "Ces principes allaient transformer le monde entier.",
    }
    _simulate_corrections(pages, corrections)
    rec = _reconcile_all_pairs(pages)
    root, rw = _rewrite_and_parse(SAMPLE_XML_PATH, pages, tmp_path)

    report = [
        "",
        "=" * 60,
        "  SPRINT 3 — DIAGNOSTIC REPORT (sample.xml)",
        "=" * 60,
        f"  Total lines:        {rw.total_lines}",
        f"  Untouched:          {rw.untouched}",
        f"  SUBS-only:          {rw.subs_only}",
        f"  Fast path:          {rw.fast_path}",
        f"  Slow path:          {rw.slow_path}",
        "-" * 60,
        f"  Hyphen pairs total: {rec.total}",
        f"    Coherent:         {rec.coherent}",
        f"    Fallback:         {rec.fallback}",
        f"    Neutralised:      {rec.neutralised}",
        "=" * 60,
        "",
    ]
    print("\n".join(report))

    # Sanity assertions
    assert rw.total_lines == 10
    assert rec.total == 3
    assert rec.fallback == 0  # all corrections are designed to be coherent
