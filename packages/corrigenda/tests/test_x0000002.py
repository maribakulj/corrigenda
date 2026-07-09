"""Sprint 4 — Validation on real BnF corpus X0000002.xml.

Tests:
  - Structure parsing (total lines, hyphen pairs, pair linkage)
  - Double-dash / soft-hyphen normalization
  - Rewriter metrics on unchanged corpus
  - Rewriter metrics with simulated corrections
  - Sensitive zone analysis (TL000014-18, TL000033-36, etc.)
  - Slow path qualification
  - Non-regression: no incoherent pair after reconciliation
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.alto.rewriter import rewrite_alto_file

from corrigenda.core.schemas import HyphenRole, LineStatus

from tests._pipeline_harness import run_pipeline

X0000002_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "examples" / "X0000002.xml"
)

pytestmark = pytest.mark.skipif(
    not X0000002_PATH.exists(), reason="X0000002.xml not found"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_lines(pages):
    return {lm.line_id: lm for pg in pages for lm in pg.lines}


# ===========================================================================
# A. Structure parsing
# ===========================================================================


class TestX0000002Structure:
    """Verify parser extracts expected structure from X0000002.xml."""

    def test_total_lines(self):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        total = sum(len(pg.lines) for pg in pages)
        assert total == 566

    def test_hyphen_pair_counts(self):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        p1 = [lm for lm in lines.values() if lm.hyphen_role == HyphenRole.PART1]
        p2 = [lm for lm in lines.values() if lm.hyphen_role == HyphenRole.PART2]
        both = [lm for lm in lines.values() if lm.hyphen_role == HyphenRole.BOTH]
        explicit = [lm for lm in p1 if lm.hyphen_source_explicit]
        heuristic = [lm for lm in p1 if not lm.hyphen_source_explicit]

        # L10/B6 — heuristic dropped from 14 to 13 after the
        # "alpha-before-dash" tightening eliminated one OCR-garbage
        # line that had a non-alpha char before its trailing dash
        # (was being detected as a phantom hyphen pair).
        assert len(p1) == 103
        assert len(explicit) == 90
        assert len(heuristic) == 13
        assert len(p2) == 99  # was 125, 25 moved to BOTH, 1 dropped by L10/B6
        assert len(both) == 26  # chained hyphenation lines

    def test_linked_pairs(self):
        """All PART1/BOTH lines with forward links have matching partners."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        # PART1 forward links
        linked_p1 = [
            lm
            for lm in lines.values()
            if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id
        ]
        # L10/B6 — one previously-linked PART1 was OCR-garbage
        # (non-alpha before trailing dash); tightened heuristic
        # correctly drops it.
        assert len(linked_p1) == 99

        for lm in linked_p1:
            partner = lines.get(lm.hyphen_pair_line_id)
            assert partner is not None, (
                f"{lm.line_id} links to missing {lm.hyphen_pair_line_id}"
            )
            assert partner.hyphen_role in (HyphenRole.PART2, HyphenRole.BOTH)

        # BOTH forward links
        linked_both = [
            lm
            for lm in lines.values()
            if lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id
        ]
        assert len(linked_both) == 26

        for lm in linked_both:
            partner = lines.get(lm.hyphen_forward_pair_id)
            assert partner is not None, (
                f"{lm.line_id} forward links to missing {lm.hyphen_forward_pair_id}"
            )
            assert partner.hyphen_role in (HyphenRole.PART2, HyphenRole.BOTH)

        # Zero unpaired PART2
        unpaired = [
            lm
            for lm in lines.values()
            if lm.hyphen_role == HyphenRole.PART2 and not lm.hyphen_pair_line_id
        ]
        assert len(unpaired) == 0


# ===========================================================================
# B. Double-dash / soft-hyphen normalization
# ===========================================================================


class TestSoftHyphenNormalization:
    """Verify soft-hyphen (\xad) is normalized to '-' in ocr_text."""

    def test_no_soft_hyphen_in_ocr_text(self):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)
        for lm in lines.values():
            assert "\u00ad" not in lm.ocr_text, (
                f"{lm.line_id}: soft-hyphen found in ocr_text: {lm.ocr_text!r}"
            )

    def test_part1_ends_with_single_dash(self):
        """Every PART1 line's ocr_text ends with exactly one '-'."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        for lm in lines.values():
            if lm.hyphen_role != HyphenRole.PART1:
                continue
            stripped = lm.ocr_text.rstrip()
            assert stripped.endswith("-"), (
                f"{lm.line_id}: PART1 does not end with dash: {lm.ocr_text!r}"
            )
            # Should NOT end with "--" (double dash from HYP+CONTENT)
            if not stripped.endswith("---"):  # skip genuine multi-dash OCR artifacts
                assert not stripped.endswith("--"), (
                    f"{lm.line_id}: PART1 ends with double dash: {lm.ocr_text!r}"
                )


# ===========================================================================
# C. Sensitive zones
# ===========================================================================


class TestSensitiveZones:
    """Verify correct parsing of historically sensitive zones."""

    def test_tl000014_necessaires(self):
        """néces-/saires explicit pair."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        p1 = lines["PAG_00000002_TL000014"]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p1.hyphen_source_explicit is True
        assert p1.hyphen_subs_content == "nécessaires"
        assert p1.ocr_text.endswith("néces-")
        assert "\u00ad" not in p1.ocr_text

        p2 = lines["PAG_00000002_TL000015"]
        assert p2.hyphen_role == HyphenRole.PART2
        assert p2.ocr_text.startswith("saires")

    def test_tl000016_praticables(self):
        """pratica-/bles explicit pair; TL000017 is BOTH (chained)."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        p1 = lines["PAG_00000002_TL000016"]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p1.hyphen_subs_content == "praticables."
        assert p1.ocr_text.endswith("pratica-")

        p2 = lines["PAG_00000002_TL000017"]
        assert (
            p2.hyphen_role == HyphenRole.BOTH
        )  # chained: PART2 of praticables + PART1 of desservent
        assert p2.hyphen_subs_content == "praticables."  # backward subs
        assert p2.hyphen_forward_subs_content == "desservent"  # forward subs

    def test_tl000033_condamne(self):
        """con-/damne explicit pair."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        p1 = lines["PAG_00000002_TL000033"]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p1.hyphen_subs_content == "condamne"
        assert p1.ocr_text.endswith("con-")

        p2 = lines["PAG_00000002_TL000034"]
        assert p2.hyphen_role == HyphenRole.PART2
        assert p2.ocr_text.startswith("damne")

    def test_tl000035_traitees(self):
        """trai-/tées explicit pair."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        p1 = lines["PAG_00000002_TL000035"]
        assert p1.hyphen_role == HyphenRole.PART1
        assert p1.hyphen_subs_content == "traitées,"


# ===========================================================================
# D. Rewriter metrics — unchanged corpus
# ===========================================================================


class TestX0000002UnchangedRewrite:
    """No corrections applied → all lines untouched."""

    def test_all_untouched(self, tmp_path):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        _xml_bytes, metrics, _paths = rewrite_alto_file(
            X0000002_PATH,
            pages,
            "test",
            "test-model",
        )
        assert metrics.total_lines == 566
        assert metrics.untouched == 566
        assert metrics.fast_path == 0
        assert metrics.slow_path == 0
        assert metrics.subs_only == 0


# ===========================================================================
# E. Rewriter metrics — simulated corrections (fast/slow path)
# ===========================================================================


class TestX0000002SimulatedCorrections:
    """Apply targeted corrections to exercise rewriter paths."""

    def _parse_and_correct(self, corrections: dict[str, str]):
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        for pg in pages:
            for lm in pg.lines:
                if lm.line_id in corrections:
                    lm.corrected_text = corrections[lm.line_id]
        return pages

    def test_fast_path_same_word_count(self, tmp_path):
        """Corrections identical to OCR → untouched."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        # No corrections at all
        _, metrics, _paths = rewrite_alto_file(
            X0000002_PATH, pages, "test", "test-model"
        )
        assert metrics.untouched == 566
        assert metrics.fast_path == 0

    def test_fast_path_real_correction(self, tmp_path):
        """Correction that changes text but keeps word count → fast path."""
        pages = self._parse_and_correct(
            {
                "PAG_00000002_TL000011": "en cet état, presque tous les chemins RU",  # changed last word
            }
        )
        _, metrics, _paths = rewrite_alto_file(
            X0000002_PATH, pages, "test", "test-model"
        )
        assert metrics.fast_path == 1
        assert metrics.slow_path == 0

    def test_slow_path_word_count_change(self, tmp_path):
        """Correction that changes word count → slow path."""
        pages = self._parse_and_correct(
            {
                "PAG_00000002_TL000011": "en cet état presque tous les chemins ruraux vicinaux",  # more words
            }
        )
        _, metrics, _paths = rewrite_alto_file(
            X0000002_PATH, pages, "test", "test-model"
        )
        assert metrics.slow_path == 1

    def test_mixed_paths(self, tmp_path):
        """Mix of unchanged, fast, and slow corrections."""
        pages = self._parse_and_correct(
            {
                # Fast path: same word count, text changed
                "PAG_00000002_TL000024": "cou s'ils y vont en voiture, à se donner UNE",
                "PAG_00000002_TL000025": "entorse s'ils y vont à pieds!",
                # Slow path: word count changed
                "PAG_00000002_TL000026": "G. Dupont.",  # 1→2 words
            }
        )
        _, metrics, _paths = rewrite_alto_file(
            X0000002_PATH, pages, "test", "test-model"
        )
        assert metrics.fast_path == 2
        assert metrics.slow_path == 1
        assert metrics.untouched == 566 - 3


# ===========================================================================
# F. Reconciliation on real corpus — no incoherent pair
# ===========================================================================


class TestX0000002ReconcileInvariant:
    """After a REAL pipeline run, no mixed pair (one OCR, one corrected)
    survives, and the reconciliation counts are those the shipping
    ``CorrectionPipeline`` produces — not a test-side re-implementation.

    Audit Phase 1: these tests drive the real pipeline via
    ``tests._pipeline_harness.run_pipeline`` (identity or targeted
    corrections through a ``DictProvider``). Injecting a fault into
    ``pipeline._reconcile_chunk_hyphens`` turns them red — which the
    former ``_reconcile_all_pairs`` phantom driver never did. The pinned
    counts (coherent=115 / neutralised=10 / fallback=0, total=125) are the
    real pipeline's output on the identity pass; a legitimate change to
    reconciliation must update them consciously here.
    """

    def _pairs_not_mixed(self, run) -> bool:
        """No hyphen pair may end with exactly one side reverted to OCR."""
        for lm in run.lines.values():
            if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
                p2 = run.lines.get(lm.hyphen_pair_line_id)
            elif lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id:
                p2 = run.lines.get(lm.hyphen_forward_pair_id)
            else:
                continue
            if p2 is None:
                continue
            p1_at_ocr = (lm.corrected_text or lm.ocr_text) == lm.ocr_text
            p2_at_ocr = (p2.corrected_text or p2.ocr_text) == p2.ocr_text
            if p1_at_ocr != p2_at_ocr:
                return False
        return True

    def test_identity_run_reconciles_every_pair_no_fallback(self):
        """Identity pass → every forward pair reconciled, zero fallback,
        zero missing partner. Pins the real coherent/neutralised split."""
        run = run_pipeline("X0000002.xml")
        rm = run.result.reconcile_metrics
        assert rm.fallback == 0
        assert rm.total == 125  # 99 PART1 + 26 BOTH forward pairs
        assert rm.coherent == 115
        assert rm.neutralised == 10
        assert run.result.total_reconciled == 125
        # Every same-page partner resolved (guards audit Problem 1).
        assert run.observer.count("hyphen_partner_missing") == 0
        assert self._pairs_not_mixed(run)

    def test_coherent_pair_necessaires(self):
        """Coherent correction for néces-/saires through the real pipeline:
        the pair is accepted, its explicit SUBS_CONTENT preserved, not mixed."""
        run = run_pipeline(
            "X0000002.xml",
            {
                "PAG_00000002_TL000014": "la municipalité prenne les mesures néces-",
                "PAG_00000002_TL000015": "saires pour y faire les réparations les plus",
            },
        )
        p1 = run.lines["PAG_00000002_TL000014"]
        p2 = run.lines["PAG_00000002_TL000015"]
        assert p1.corrected_text == "la municipalité prenne les mesures néces-"
        assert p1.hyphen_subs_content == "nécessaires"  # explicit subs preserved
        assert p2.corrected_text == "saires pour y faire les réparations les plus"
        assert self._pairs_not_mixed(run)

    def test_no_mixed_pair_after_incoherent_correction(self):
        """Incoherent correction (con+tinue ≠ condamne) → the real pipeline
        neutralises BOTH sides back to OCR and drops the SUBS_CONTENT."""
        run = run_pipeline(
            "X0000002.xml",
            {
                "PAG_00000002_TL000033": "crient : « Vive la liberté ! » quand on con-",
                "PAG_00000002_TL000034": "tinue les patriotes qui crient : « Vive",
            },
        )
        p1 = run.lines["PAG_00000002_TL000033"]
        p2 = run.lines["PAG_00000002_TL000034"]
        # Both reverted to OCR text; SUBS neutralised; pair not mixed.
        assert p1.corrected_text == p1.ocr_text
        assert p2.corrected_text == p2.ocr_text
        assert p1.hyphen_subs_content is None
        assert self._pairs_not_mixed(run)
        # LineStatus is a real enum on both sides (regression guard on the
        # neutralise-to-OCR path leaving a coherent, non-fallback status).
        assert isinstance(p1.status, LineStatus)


# ===========================================================================
# G. Diagnostic report
# ===========================================================================


def test_x0000002_diagnostic_report(tmp_path, capsys):
    """Print diagnostic report for X0000002.xml."""
    pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")

    total_lines = sum(len(pg.lines) for pg in pages)
    lines = _all_lines(pages)

    p1_all = [lm for lm in lines.values() if lm.hyphen_role == HyphenRole.PART1]
    p2_all = [lm for lm in lines.values() if lm.hyphen_role == HyphenRole.PART2]
    both_all = [lm for lm in lines.values() if lm.hyphen_role == HyphenRole.BOTH]
    linked_p1 = [lm for lm in p1_all if lm.hyphen_pair_line_id]
    linked_both = [lm for lm in both_all if lm.hyphen_forward_pair_id]
    unpaired_p2 = [lm for lm in p2_all if not lm.hyphen_pair_line_id]

    # Soft-hyphen check
    soft_hyp = [lm for lm in lines.values() if "\u00ad" in lm.ocr_text]

    # Reconcile via the REAL pipeline (identity pass), not a test copy.
    rec = run_pipeline("X0000002.xml").result.reconcile_metrics

    # Rewriter with no corrections
    _, rw, _paths = rewrite_alto_file(X0000002_PATH, pages, "test", "test-model")

    report = [
        "",
        "=" * 65,
        "  SPRINT 5 — DIAGNOSTIC REPORT (X0000002.xml)",
        "=" * 65,
        f"  Total lines:              {total_lines}",
        f"  PART1 (explicit):         {sum(1 for l in p1_all if l.hyphen_source_explicit)}",
        f"  PART1 (heuristic):        {sum(1 for l in p1_all if not l.hyphen_source_explicit)}",
        f"  PART2:                    {len(p2_all)}",
        f"  BOTH (chained):           {len(both_all)}",
        f"  Linked PART1→partner:     {len(linked_p1)}",
        f"  Linked BOTH→forward:      {len(linked_both)}",
        f"  Total forward pairs:      {len(linked_p1) + len(linked_both)}",
        f"  Unpaired PART2 (orphans): {len(unpaired_p2)}",
        "-" * 65,
        f"  Soft-hyphen in ocr_text:  {len(soft_hyp)}",
        "-" * 65,
        f"  Rewriter — untouched:     {rw.untouched}",
        f"  Rewriter — subs-only:     {rw.subs_only}",
        f"  Rewriter — fast path:     {rw.fast_path}",
        f"  Rewriter — slow path:     {rw.slow_path}",
        "-" * 65,
        f"  Reconcile — total pairs:  {rec.total}",
        f"    Coherent:               {rec.coherent}",
        f"    Fallback:               {rec.fallback}",
        f"    Neutralised:            {rec.neutralised}",
        "=" * 65,
        "",
    ]
    print("\n".join(report))

    # Sanity assertions
    assert total_lines == 566
    assert len(soft_hyp) == 0, "Soft-hyphen not fully normalized"
    assert len(unpaired_p2) == 0, "Orphan PART2 still present"
    assert len(both_all) == 26, "Chained lines not detected"
    assert rw.untouched == 566
    assert rec.fallback == 0
