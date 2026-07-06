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
from corrigenda.core.hyphenation import (
    ReconcileMetrics,
    classify_reconcile_outcome,
    reconcile_hyphen_pair,
)
from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.alto.rewriter import rewrite_alto_file

from corrigenda.core.schemas import HyphenRole

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


def _reconcile_all_pairs(pages) -> ReconcileMetrics:
    from copy import copy

    metrics = ReconcileMetrics()
    for page in pages:
        line_by_id = {lm.line_id: lm for lm in page.lines}
        for lm in page.lines:
            # PART1 forward pairs
            if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
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
            # BOTH forward pairs
            elif lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id:
                part2 = line_by_id.get(lm.hyphen_forward_pair_id)
                if part2 is None:
                    continue
                corrected_p1 = lm.corrected_text or lm.ocr_text
                corrected_p2 = part2.corrected_text or part2.ocr_text

                lm_as_p1 = copy(lm)
                lm_as_p1.hyphen_role = HyphenRole.PART1
                lm_as_p1.hyphen_subs_content = lm.hyphen_forward_subs_content
                lm_as_p1.hyphen_source_explicit = lm.hyphen_forward_explicit

                final_p1, final_p2, subs = reconcile_hyphen_pair(
                    lm_as_p1,
                    part2,
                    corrected_p1,
                    corrected_p2,
                )
                lm.corrected_text = final_p1
                lm.hyphen_forward_subs_content = subs
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
            else:
                continue

            if outcome == "coherent":
                metrics.coherent += 1
            elif outcome == "fallback":
                metrics.fallback += 1
            else:
                metrics.neutralised += 1
    return metrics


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
    """After reconciliation, no mixed pair (one OCR, one corrected) exists."""

    def test_unchanged_reconcile(self):
        """No corrections → all pairs neutralised (OCR=correction)."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        rec = _reconcile_all_pairs(pages)
        # All pairs: correction == OCR text → neutralised
        assert rec.fallback == 0
        # 99 PART1 (was 100, -1 from L10/B6 corpus invariant change) + 26 BOTH
        assert rec.total == 125

    def test_coherent_pair_necessaires(self):
        """Simulated coherent correction for néces-/saires."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        # Correct both sides preserving structure
        lines[
            "PAG_00000002_TL000014"
        ].corrected_text = "la municipalité prenne les mesures néces-"
        lines[
            "PAG_00000002_TL000015"
        ].corrected_text = "saires pour y faire les réparations les plus"

        rec = _reconcile_all_pairs(pages)
        # This specific pair should be coherent (join matches subs)
        p1 = lines["PAG_00000002_TL000014"]
        assert p1.hyphen_subs_content == "nécessaires"

    def test_no_mixed_pair_after_incoherent_correction(self):
        """Simulate an incoherent LLM response → verify both fall back."""
        pages, _ = parse_alto_file(X0000002_PATH, "X0000002.xml")
        lines = _all_lines(pages)

        # Make TL000033 (con-) incoherent: change boundary word
        lines[
            "PAG_00000002_TL000033"
        ].corrected_text = "crient : « Vive la liberté ! » quand on con-"
        lines[
            "PAG_00000002_TL000034"
        ].corrected_text = (
            "tinue les patriotes qui crient : « Vive"  # con+tinue ≠ condamne
        )

        _reconcile_all_pairs(pages)

        p1 = lines["PAG_00000002_TL000033"]
        p2 = lines["PAG_00000002_TL000034"]
        # Both must be at OCR (fallback)
        assert p1.corrected_text == p1.ocr_text
        assert p2.corrected_text == p2.ocr_text
        # SUBS neutralised
        assert p1.hyphen_subs_content is None


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

    # Reconcile with no corrections
    rec = _reconcile_all_pairs(pages)

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
