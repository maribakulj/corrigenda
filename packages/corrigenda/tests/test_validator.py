"""Tests for jobs/validator.py"""

from __future__ import annotations

import pytest
from corrigenda.core.validator import validate_llm_response

from corrigenda.core.schemas import ProposalBatch

# ---------------------------------------------------------------------------
# test_valid_response
# ---------------------------------------------------------------------------


def test_valid_response():
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "Bonjour monde"},
            {"line_id": "L2", "corrected_text": "Voici le texte"},
        ]
    }
    result = validate_llm_response(raw, ["L1", "L2"])
    assert isinstance(result, ProposalBatch)
    assert len(result.lines) == 2
    assert result.lines[0].line_id == "L1"
    assert result.lines[0].corrected_text == "Bonjour monde"


# ---------------------------------------------------------------------------
# test_missing_lines_key
# ---------------------------------------------------------------------------


def test_missing_lines_key():
    with pytest.raises(ValueError, match="Missing key 'lines'"):
        validate_llm_response({"data": []}, ["L1"])


# ---------------------------------------------------------------------------
# test_missing_line_id
# ---------------------------------------------------------------------------


def test_missing_line_id():
    raw = {
        "lines": [
            {"corrected_text": "some text"},
        ]
    }
    with pytest.raises(ValueError, match="missing 'line_id'"):
        validate_llm_response(raw, ["L1"])


# ---------------------------------------------------------------------------
# test_duplicate_line_id
# ---------------------------------------------------------------------------


def test_duplicate_line_id():
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "text one"},
            {"line_id": "L1", "corrected_text": "text two"},
        ]
    }
    with pytest.raises(ValueError, match="Duplicate line_id"):
        validate_llm_response(raw, ["L1", "L2"])


# ---------------------------------------------------------------------------
# test_unknown_line_id
# ---------------------------------------------------------------------------


def test_unknown_line_id():
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "hello"},
            {"line_id": "L_UNKNOWN", "corrected_text": "world"},
        ]
    }
    with pytest.raises(ValueError, match="Unknown line_id"):
        validate_llm_response(raw, ["L1", "L2"])


# ---------------------------------------------------------------------------
# test_newline_in_text
# ---------------------------------------------------------------------------


def test_newline_in_text():
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "hello\nworld"},
        ]
    }
    with pytest.raises(ValueError, match="line separator"):
        validate_llm_response(raw, ["L1"])


# ---------------------------------------------------------------------------
# test_empty_corrected_text
# ---------------------------------------------------------------------------


def test_empty_corrected_text():
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": ""},
        ]
    }
    with pytest.raises(ValueError, match="empty"):
        validate_llm_response(raw, ["L1"])


# ---------------------------------------------------------------------------
# test_count_mismatch
# ---------------------------------------------------------------------------


def test_count_mismatch():
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "hello"},
        ]
    }
    with pytest.raises(ValueError, match="count mismatch"):
        validate_llm_response(raw, ["L1", "L2"])


# ---------------------------------------------------------------------------
# test_hyphen_part2_empty_violation
# ---------------------------------------------------------------------------


def test_hyphen_part2_empty_violation():
    raw_empty = {
        "lines": [
            {"line_id": "L1", "corrected_text": "por-"},
            {
                "line_id": "L2",
                "corrected_text": "",
            },  # empty → base validation catches it
        ]
    }
    with pytest.raises(ValueError):
        validate_llm_response(
            raw_empty,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
        )


# ---------------------------------------------------------------------------
# test_hyphen_part1_fusion_violation
# ---------------------------------------------------------------------------


def test_hyphen_part1_fusion_violation():
    """PART1 corrected_text stripped of '-' equals full subs_content → fusion."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "porte"},  # full word, no hyphen
            {"line_id": "L2", "corrected_text": "ouverte"},
        ]
    }
    with pytest.raises(ValueError, match="hyphen_integrity_violation"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
            hyphen_subs={"L1": "porte"},
        )


# ---------------------------------------------------------------------------
# test_hyphen_part1_drift_violation
# ---------------------------------------------------------------------------


def test_hyphen_part1_drift_violation():
    """PART1 grew by more than 2 words → text migration suspected."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "nécessaires pour y faire"},
            {"line_id": "L2", "corrected_text": "suite du texte"},
        ]
    }
    with pytest.raises(ValueError, match="hyphen_integrity_violation"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
            ocr_texts={"L1": "néces-", "L2": "saires pour y faire suite du texte"},
        )


# ---------------------------------------------------------------------------
# test_hyphen_fusion_multiword_part1
# ---------------------------------------------------------------------------


def test_hyphen_fusion_multiword_part1():
    """PART1 with multiple words — last word equals subs → fusion detected."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "Il nécessaires"},
            {"line_id": "L2", "corrected_text": "pour y faire"},
        ]
    }
    with pytest.raises(ValueError, match="hyphen_integrity_violation.*fusion"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
            hyphen_subs={"L1": "nécessaires"},
        )


# ---------------------------------------------------------------------------
# Unicode NFC vs NFD fusion detection (B-013)
# ---------------------------------------------------------------------------

import unicodedata


def test_hyphen_fusion_detected_when_subs_is_nfd():
    """LLM PART1 in NFC must match subs_content given in NFD — both forms
    of 'nécessaires' compare equal after _norm.ncfold."""
    nfd = unicodedata.normalize("NFD", "nécessaires")
    assert nfd != "nécessaires"  # sanity: forms really differ

    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "nécessaires"},  # NFC
            {"line_id": "L2", "corrected_text": "pour y faire"},
        ]
    }
    with pytest.raises(ValueError, match="hyphen_integrity_violation"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
            hyphen_subs={"L1": nfd},  # subs in NFD
        )


def test_hyphen_fusion_detected_when_corrected_is_nfd():
    """Mirror case: LLM emits NFD, subs_content is NFC."""
    nfd_word = unicodedata.normalize("NFD", "nécessaires")
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": nfd_word},
            {"line_id": "L2", "corrected_text": "pour y faire"},
        ]
    }
    with pytest.raises(ValueError, match="hyphen_integrity_violation"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
            hyphen_subs={"L1": "nécessaires"},
        )


# ---------------------------------------------------------------------------
# Fusion is a DRIFT check — the source text is the reference
# ---------------------------------------------------------------------------


def test_fusion_not_flagged_when_source_already_ends_with_the_word():
    """An identity proposal must never be rejected as fusion.

    Degenerate one-letter fragments make it real: a BOTH line carrying
    'A' (PART2 of the previous pair) + 'A' (PART1 of the next, logical
    word 'AA') reads 'AA-' in the SOURCE — its last word already equals
    the full logical word before the LLM says anything. Flagging that is
    a false positive with a hard-fail blast radius: the chunk retries
    deterministically, exhausts its budget and OCR-fallbacks every
    cohabiting line, which the chunking-invariance gate exposed as a
    partition-dependent outcome."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "AA-"},
            {"line_id": "L2", "corrected_text": "A"},
        ]
    }
    resp = validate_llm_response(
        raw,
        ["L1", "L2"],
        hyphen_pairs={"L1": "L2", "L2": "L1"},
        ocr_texts={"L1": "AA-", "L2": "A"},
        hyphen_subs={"L1": "AA"},
    )
    assert [ln.corrected_text for ln in resp.lines] == ["AA-", "A"]


def test_fusion_still_flagged_when_only_the_correction_ends_with_the_word():
    """The genuine case keeps failing: the source ends with the fragment
    ('por-'), only the CORRECTION contains the full word — that is the
    LLM merging the pair, exactly what the check exists for."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "porte"},
            {"line_id": "L2", "corrected_text": "ouverte"},
        ]
    }
    with pytest.raises(ValueError, match="hyphen_integrity_violation.*fusion"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            hyphen_pairs={"L1": "L2", "L2": "L1"},
            ocr_texts={"L1": "por-", "L2": "te ouverte"},
            hyphen_subs={"L1": "porte"},
        )


# ---------------------------------------------------------------------------
# F8 — target-based counting (spec §7-F8: the 1:1 count is on targets)
# ---------------------------------------------------------------------------


def test_targets_mode_missing_context_output_is_accepted():
    """A response omitting a CONTEXT line is valid: only targets are
    required. Historical mode (no targets) would reject on count."""
    raw = {"lines": [{"line_id": "L1", "corrected_text": "un"}]}
    resp = validate_llm_response(
        raw,
        ["L1", "L2"],  # L2 sent as context
        target_line_ids=["L1"],
    )
    assert [o.line_id for o in resp.lines] == ["L1"]


def test_targets_mode_missing_target_still_rejected():
    raw = {"lines": [{"line_id": "L2", "corrected_text": "deux"}]}
    with pytest.raises(ValueError, match="Missing line_ids"):
        validate_llm_response(
            raw,
            ["L1", "L2"],
            target_line_ids=["L1"],
        )


def test_targets_mode_context_entry_present_is_kept_and_checked():
    """Context entries, when present, pass through (downstream discards
    them) but stay structurally checked — garbage anywhere is a degraded
    response."""
    raw = {
        "lines": [
            {"line_id": "L1", "corrected_text": "un"},
            {"line_id": "L2", "corrected_text": "deux"},
        ]
    }
    resp = validate_llm_response(raw, ["L1", "L2"], target_line_ids=["L1"])
    assert {o.line_id for o in resp.lines} == {"L1", "L2"}

    bad = {
        "lines": [
            {"line_id": "L1", "corrected_text": "un"},
            {"line_id": "L2", "corrected_text": "   "},  # whitespace-only
        ]
    }
    with pytest.raises(ValueError, match="empty or missing"):
        validate_llm_response(bad, ["L1", "L2"], target_line_ids=["L1"])


def test_no_targets_arg_is_byte_compatible_with_historical_count():
    """target_line_ids=None keeps the exact-count contract."""
    raw = {"lines": [{"line_id": "L1", "corrected_text": "un"}]}
    with pytest.raises(ValueError, match="Line count mismatch"):
        validate_llm_response(raw, ["L1", "L2"])
