"""Tests for alto/hyphenation.py"""

from __future__ import annotations

from corrigenda.alto.hyphenation import (
    enrich_chunk_lines,
    reconcile_hyphen_pair,
    should_stay_in_same_chunk,
)

from corrigenda.schemas import Coords, HyphenRole, LineManifest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_line(
    line_id: str,
    ocr_text: str,
    hyphen_role: HyphenRole = HyphenRole.NONE,
    hyphen_pair_line_id: str | None = None,
    hyphen_subs_content: str | None = None,
    hyphen_source_explicit: bool = False,
    prev_line_id: str | None = None,
    next_line_id: str | None = None,
    hyphen_forward_pair_id: str | None = None,
    hyphen_forward_subs_content: str | None = None,
    hyphen_forward_explicit: bool = False,
) -> LineManifest:
    return LineManifest(
        line_id=line_id,
        page_id="P1",
        block_id="TB1",
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=0, vpos=0, width=200, height=20),
        ocr_text=ocr_text,
        hyphen_role=hyphen_role,
        hyphen_pair_line_id=hyphen_pair_line_id,
        hyphen_subs_content=hyphen_subs_content,
        hyphen_source_explicit=hyphen_source_explicit,
        prev_line_id=prev_line_id,
        next_line_id=next_line_id,
        hyphen_forward_pair_id=hyphen_forward_pair_id,
        hyphen_forward_subs_content=hyphen_forward_subs_content,
        hyphen_forward_explicit=hyphen_forward_explicit,
    )


# ---------------------------------------------------------------------------
# enrich_chunk_lines
# ---------------------------------------------------------------------------


def test_enrich_part1_has_join_with_next():
    part1 = make_line(
        "TL1",
        "Il por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=True,
        next_line_id="TL2",
    )
    part2 = make_line(
        "TL2",
        "te ouverte",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=True,
        prev_line_id="TL1",
    )
    all_lines = {"TL1": part1, "TL2": part2}
    result = enrich_chunk_lines([part1, part2], all_lines)

    inp1 = result[0]
    assert inp1.hyphen_join_with_next is True
    assert inp1.hyphen_join_with_prev is None
    assert inp1.hyphenation_role == "HypPart1"
    assert inp1.hyphen_candidate is True


def test_enrich_part2_has_join_with_prev():
    part1 = make_line(
        "TL1",
        "Il por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        next_line_id="TL2",
    )
    part2 = make_line(
        "TL2",
        "te ouverte",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        prev_line_id="TL1",
    )
    all_lines = {"TL1": part1, "TL2": part2}
    result = enrich_chunk_lines([part1, part2], all_lines)

    inp2 = result[1]
    assert inp2.hyphen_join_with_prev is True
    assert inp2.hyphen_join_with_next is None
    assert inp2.hyphenation_role == "HypPart2"
    assert inp2.hyphen_candidate is True


def test_enrich_logical_candidate_present_when_known():
    part1 = make_line(
        "TL1",
        "por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="porte",
        hyphen_source_explicit=True,
    )
    all_lines = {"TL1": part1}
    result = enrich_chunk_lines([part1], all_lines)
    assert result[0].forward_join_candidate == "porte"


def test_enrich_no_hyphen_fields_on_normal_line():
    line = make_line("TL1", "Texte normal.")
    result = enrich_chunk_lines([line], {"TL1": line})
    inp = result[0]
    assert inp.hyphenation_role is None
    assert inp.hyphen_candidate is None
    assert inp.hyphen_join_with_next is None
    assert inp.hyphen_join_with_prev is None
    assert inp.backward_join_candidate is None
    assert inp.forward_join_candidate is None


# ---------------------------------------------------------------------------
# reconcile_hyphen_pair — Explicit mode
# ---------------------------------------------------------------------------


def test_reconcile_explicit_preserves_boundaries():
    """Explicit pair with known SUBS_CONTENT: boundaries stay, subs resolved."""
    part1 = make_line(
        "TL1",
        "Il por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="porte",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "te ouverte",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="porte",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "Il por-", "te ouverte")
    assert t1 == "Il por-"
    assert t2 == "te ouverte"
    assert subs == "porte"


def test_reconcile_explicit_llm_completed_word():
    """Explicit: LLM completed hyphenated word → BOTH sides fall back,
    SUBS_CONTENT neutralised."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faire",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "nécessaires", "pour y faire")
    assert t1 == "néces-", "PART1 must fall back to OCR source"
    assert t2 == "saires pour y faire", "PART2 must also fall back to OCR source"
    assert subs is None, "SUBS_CONTENT must be neutralised"


def test_reconcile_explicit_subs_mismatch_falls_back():
    """Explicit: LLM kept the dash but gave PART2 a wrong first word.
    Join doesn't match subs_content → BOTH sides fall back to OCR."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faire",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    # LLM replaced PART2 first word with "urgentes"
    t1, t2, subs = reconcile_hyphen_pair(
        part1, part2, "néces-", "urgentes pour y faire"
    )
    assert t1 == "néces-", "PART1 must fall back to OCR"
    assert t2 == "saires pour y faire", "PART2 must fall back to OCR"
    assert subs is None, "SUBS_CONTENT must be neutralised"


def test_reconcile_explicit_part2_absurd_word():
    """Explicit: PART2 replaced with a completely unrelated word like 'la'.
    This is the documented bug — must never produce a mixed incoherent pair."""
    part1 = make_line(
        "TL1",
        "pratica-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="praticables",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "bles du chemin",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="praticables",
        hyphen_source_explicit=True,
    )
    # LLM replaced PART2 first word with "la"
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "pratica-", "la du chemin")
    assert t1 == "pratica-", "PART1 → OCR source"
    assert t2 == "bles du chemin", "PART2 → OCR source"
    assert subs is None


def test_reconcile_explicit_condamne_coherent():
    """Explicit: 'con- / damne' with correct LLM output → accepted."""
    part1 = make_line(
        "TL1",
        "con-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="condamne",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "damne à mort",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="condamne",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "con-", "damne à mort")
    assert t1 == "con-"
    assert t2 == "damne à mort"
    assert subs == "condamne"


def test_reconcile_explicit_condamne_fusion():
    """Explicit: LLM fused 'con- / damne' into 'condamne' → both fall back."""
    part1 = make_line(
        "TL1",
        "con-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="condamne",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "damne à mort",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="condamne",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "condamne", "à mort")
    assert t1 == "con-", "PART1 → OCR source"
    assert t2 == "damne à mort", "PART2 → OCR source"
    assert subs is None


def test_reconcile_explicit_coherent_correction():
    """Explicit: LLM corrected OCR errors but kept the split coherent."""
    part1 = make_line(
        "TL1",
        "nôccs-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faiie",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "néces-", "saires pour y faire")
    assert t1 == "néces-"
    assert t2 == "saires pour y faire"
    assert subs == "nécessaires"


def test_reconcile_explicit_part1_lost_dash():
    """Explicit: LLM dropped the trailing dash → both fall back."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faire",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "néces", "saires pour y faire")
    assert t1 == "néces-", "PART1 → OCR source (dash was lost)"
    assert t2 == "saires pour y faire", "PART2 → OCR source"
    assert subs is None


def test_reconcile_ambiguous_returns_no_subs():
    """When LLM join doesn't match subs_content → BOTH sides fall back."""
    part1 = make_line(
        "TL1",
        "tra-",
        hyphen_role=HyphenRole.PART1,
        hyphen_subs_content="travail",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "vauxxx",
        hyphen_role=HyphenRole.PART2,
        hyphen_subs_content="travail",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "tra-", "vauxxx")
    assert subs is None
    # Both fall back to OCR source (OCR == corrected in this case)
    assert t1 == "tra-"
    assert t2 == "vauxxx"


def test_reconcile_explicit_no_subs_boundary_ok():
    """Explicit without subs_content: PART1 has dash, boundary word OK → accepted."""
    part1 = make_line(
        "TL1",
        "por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content=None,
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "te ouverte",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "por-", "te ouverte")
    assert t1 == "por-"
    assert t2 == "te ouverte"
    assert subs is None


def test_reconcile_explicit_no_subs_boundary_diverged():
    """Explicit without subs_content: boundary word diverged → both fall back."""
    part1 = make_line(
        "TL1",
        "por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content=None,
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "te ouverte",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=True,
    )
    # LLM changed PART2 first word from "te" to "la" (completely different)
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "por-", "la ouverte")
    assert t1 == "por-", "PART1 → OCR source"
    assert t2 == "te ouverte", "PART2 → OCR source"
    assert subs is None


# ---------------------------------------------------------------------------
# reconcile_hyphen_pair — Heuristic mode
# ---------------------------------------------------------------------------


def test_reconcile_heuristic_conservative():
    """Heuristic pair: subs_content must be None, corrected texts returned as-is."""
    part1 = make_line(
        "TL1",
        "boule-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "vard du roi",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "boule-", "vard du roi")
    assert t1 == "boule-"
    assert t2 == "vard du roi"
    assert subs is None


def test_reconcile_heuristic_llm_completed_word():
    """Heuristic: LLM completed hyphenated word → BOTH sides fall back."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faire",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "nécessaires", "pour y faire")
    assert t1 == "néces-", "PART1 must fall back to OCR source"
    assert t2 == "saires pour y faire", "PART2 must also fall back"
    assert subs is None


def test_reconcile_heuristic_llm_completed_word_part2_also_bad():
    """Heuristic: LLM completed word AND PART2 correction is invalid → both fall back."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faire",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "nécessaires", "")
    assert t1 == "néces-"
    assert t2 == "saires pour y faire"
    assert subs is None


def test_reconcile_heuristic_normal_case_unchanged():
    """Heuristic: LLM preserved the trailing dash → correction accepted."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "saires pour y faire",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "néces-", "saires pour y faire")
    assert t1 == "néces-"
    assert t2 == "saires pour y faire"
    assert subs is None


def test_reconcile_heuristic_tiret_preserved():
    """Heuristic: LLM kept the trailing dash → corrections accepted as-is."""
    part1 = make_line(
        "TL1",
        "Rus-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "sie le tsar.",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "Rus-", "sie le tsar.")
    assert t1 == "Rus-"
    assert t2 == "sie le tsar."
    assert subs is None


def test_reconcile_heuristic_part1_lost_dash():
    """Heuristic: LLM dropped the trailing dash → both fall back."""
    part1 = make_line(
        "TL1",
        "Rus-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "sie le tsar.",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "Rus", "sie le tsar.")
    assert t1 == "Rus-", "PART1 → OCR source (dash was lost)"
    assert t2 == "sie le tsar.", "PART2 → OCR source"
    assert subs is None


def test_reconcile_heuristic_boundary_word_diverged():
    """Heuristic: PART2 first word completely changed → both fall back."""
    part1 = make_line(
        "TL1",
        "boule-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_source_explicit=False,
    )
    part2 = make_line(
        "TL2",
        "vard du roi",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_source_explicit=False,
    )
    # LLM replaced "vard" with "I'armée"
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "boule-", "I'armée du roi")
    assert t1 == "boule-", "PART1 → OCR source"
    assert t2 == "vard du roi", "PART2 → OCR source"
    assert subs is None


# ---------------------------------------------------------------------------
# reconcile_hyphen_pair — No-fusion / no-merge invariant
# ---------------------------------------------------------------------------


def test_reconcile_no_line_fusion():
    """Result must always be two distinct non-empty strings."""
    part1 = make_line(
        "TL1",
        "con-",
        hyphen_role=HyphenRole.PART1,
        hyphen_subs_content="construction",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "struction solide",
        hyphen_role=HyphenRole.PART2,
        hyphen_subs_content="construction",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(part1, part2, "con-", "struction solide")
    assert t1 and t2
    assert t1 != t2
    assert "struction" not in t1
    assert "con-" not in t2


def test_reconcile_cascade_both_sides_fallback():
    """When PART1 pulls text from PART2, BOTH sides fall back."""
    part1 = make_line(
        "TL1",
        "la plate-forme sur laquelle le cou-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="couronnement",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "ronnement va avoir lieu.",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="couronnement",
        hyphen_source_explicit=True,
    )
    t1, t2, subs = reconcile_hyphen_pair(
        part1,
        part2,
        "la plate-forme sur laquelle le couronnement va avoir lieu.-",
        "ronnement va avoir lieu.",
    )
    assert t1 == "la plate-forme sur laquelle le cou-", "PART1 → OCR source"
    assert t2 == "ronnement va avoir lieu.", "PART2 → OCR source"
    assert subs is None


# ---------------------------------------------------------------------------
# reconcile_hyphen_pair — SUBS_CONTENT neutralisation
# ---------------------------------------------------------------------------


def test_reconcile_subs_content_neutralised_on_mismatch():
    """SUBS_CONTENT must be None when the pair is incoherent,
    even if a subs_content was originally known."""
    part1 = make_line(
        "TL1",
        "pratica-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="praticables",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "bles dans ce terrain",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="praticables",
        hyphen_source_explicit=True,
    )
    # LLM changed PART2 to start with "urgentes" instead of "bles"
    _, _, subs = reconcile_hyphen_pair(
        part1, part2, "pratica-", "urgentes dans ce terrain"
    )
    assert subs is None, "SUBS_CONTENT must be neutralised on incoherent pair"


def test_reconcile_subs_content_preserved_when_coherent():
    """SUBS_CONTENT must be preserved when pair is coherent."""
    part1 = make_line(
        "TL1",
        "pratica-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="praticables",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "bles dans ce terrain",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="praticables",
        hyphen_source_explicit=True,
    )
    _, _, subs = reconcile_hyphen_pair(part1, part2, "pratica-", "bles dans ce terrain")
    assert subs == "praticables"


# ---------------------------------------------------------------------------
# should_stay_in_same_chunk
# ---------------------------------------------------------------------------


def test_should_stay_linked_pair():
    part1 = make_line(
        "TL1",
        "por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
    )
    part2 = make_line(
        "TL2",
        "te",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
    )
    assert should_stay_in_same_chunk(part1, part2) is True
    assert should_stay_in_same_chunk(part2, part1) is True


def test_should_stay_unrelated_lines():
    line_a = make_line("TL1", "Bonjour monde.")
    line_b = make_line("TL2", "Autre ligne.")
    assert should_stay_in_same_chunk(line_a, line_b) is False


def test_should_stay_part1_wrong_pair_id():
    part1 = make_line(
        "TL1",
        "por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL99",
    )
    part2 = make_line(
        "TL2",
        "te",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
    )
    assert should_stay_in_same_chunk(part1, part2) is False


# ---------------------------------------------------------------------------
# BOTH symmetry: enrich_chunk_lines exposes both join candidates
# ---------------------------------------------------------------------------


def test_enrich_both_exposes_backward_and_forward_candidates():
    """A BOTH line must carry both backward and forward join candidates."""
    both = make_line(
        "TL2",
        "saires pour les me-",
        hyphen_role=HyphenRole.BOTH,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",  # backward (PART2 side)
        hyphen_source_explicit=True,
        hyphen_forward_pair_id="TL3",
        hyphen_forward_subs_content="mesures",  # forward (PART1 side)
        hyphen_forward_explicit=True,
    )
    all_lines = {"TL2": both}
    result = enrich_chunk_lines([both], all_lines)
    inp = result[0]

    assert inp.hyphenation_role == "HypBoth"
    assert inp.hyphen_join_with_prev is True
    assert inp.hyphen_join_with_next is True
    assert inp.backward_join_candidate == "nécessaires"
    assert inp.forward_join_candidate == "mesures"


def test_enrich_both_none_candidates_when_heuristic():
    """Heuristic BOTH line: both candidates are None if no subs available."""
    both = make_line(
        "TL2",
        "saires pour les me-",
        hyphen_role=HyphenRole.BOTH,
        hyphen_pair_line_id="TL1",
        hyphen_forward_pair_id="TL3",
    )
    all_lines = {"TL2": both}
    result = enrich_chunk_lines([both], all_lines)
    inp = result[0]

    assert inp.backward_join_candidate is None
    assert inp.forward_join_candidate is None
    assert inp.hyphen_join_with_prev is True
    assert inp.hyphen_join_with_next is True


def test_enrich_part1_uses_forward_candidate_only():
    """PART1 line should only have forward_join_candidate set."""
    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    result = enrich_chunk_lines([part1], {"TL1": part1})
    inp = result[0]
    assert inp.forward_join_candidate == "nécessaires"
    assert inp.backward_join_candidate is None


def test_enrich_part2_uses_backward_candidate_only():
    """PART2 line should only have backward_join_candidate set."""
    part2 = make_line(
        "TL2",
        "saires ensuite",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    result = enrich_chunk_lines([part2], {"TL2": part2})
    inp = result[0]
    assert inp.backward_join_candidate == "nécessaires"
    assert inp.forward_join_candidate is None


# ---------------------------------------------------------------------------
# reconcile_hyphen_pair with explicit subs_content/source_explicit
# ---------------------------------------------------------------------------


def test_reconcile_explicit_params_override_manifest():
    """Passing subs_content/source_explicit overrides manifest fields."""
    both_line = make_line(
        "TL2",
        "saires pour les me-",
        hyphen_role=HyphenRole.BOTH,
        hyphen_subs_content="nécessaires",  # backward subs (should be ignored)
        hyphen_source_explicit=True,
        hyphen_forward_subs_content="mesures",
        hyphen_forward_explicit=True,
    )
    part2 = make_line(
        "TL3",
        "sures nécessaires",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL2",
    )

    # Call with explicit forward params — should use "mesures" not "nécessaires"
    final_p1, final_p2, subs = reconcile_hyphen_pair(
        both_line,
        part2,
        "saires pour les me-",
        "sures nécessaires",
        subs_content="mesures",
        source_explicit=True,
    )
    assert subs == "mesures"
    assert final_p1 == "saires pour les me-"
    assert final_p2 == "sures nécessaires"


def test_reconcile_without_explicit_params_uses_manifest():
    """Without explicit params, reconcile reads from manifest (backward compat)."""
    part1 = make_line(
        "TL1",
        "Il por-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="porte",
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "te la valise",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
    )
    final_p1, final_p2, subs = reconcile_hyphen_pair(
        part1, part2, "Il por-", "te la valise"
    )
    assert subs == "porte"


# ---------------------------------------------------------------------------
# Unicode NFC/NFD comparison invariance (B-013)
# ---------------------------------------------------------------------------

import unicodedata


def test_reconcile_explicit_accepts_nfd_join_against_nfc_subs():
    """LLM emits 'né' in NFD; subs_content is 'nécessaires' in NFC.
    The join must still match after ncfold normalization."""
    nfd_nec = unicodedata.normalize("NFD", "néces")
    nfd_saires = unicodedata.normalize("NFD", "saires")
    assert nfd_nec != "néces"  # sanity

    part1 = make_line(
        "TL1",
        "néces-",
        hyphen_role=HyphenRole.PART1,
        hyphen_pair_line_id="TL2",
        hyphen_subs_content="nécessaires",  # NFC
        hyphen_source_explicit=True,
    )
    part2 = make_line(
        "TL2",
        "saires pour vivre",
        hyphen_role=HyphenRole.PART2,
        hyphen_pair_line_id="TL1",
        hyphen_subs_content="nécessaires",
        hyphen_source_explicit=True,
    )
    # Corrected text in NFD on both sides
    t1, t2, subs = reconcile_hyphen_pair(
        part1,
        part2,
        f"{nfd_nec}-",  # 'néces-' NFD
        f"{nfd_saires} pour vivre",  # 'saires pour vivre' NFD
    )
    # Pre-fix: comparison failed, both fell back. Post-fix: coherent.
    assert subs == "nécessaires"
    assert t1.endswith("-")
    assert t2.startswith(nfd_saires)


def test_boundary_word_diverged_invariant_to_normalization_form():
    """Comparing OCR 'café' (NFC) to corrected 'café' (NFD) must NOT flag divergence."""
    # Private helper of the reconciler — pulled from corrigenda directly
    # since the backend re-export shim no longer surfaces privates.
    from corrigenda.alto.hyphenation import _part2_boundary_word_diverged

    nfc_text = "café au lait"
    nfd_text = unicodedata.normalize("NFD", "café") + " au lait"
    assert nfc_text != nfd_text  # sanity

    assert _part2_boundary_word_diverged(nfc_text, nfd_text) is False
    assert _part2_boundary_word_diverged(nfd_text, nfc_text) is False
