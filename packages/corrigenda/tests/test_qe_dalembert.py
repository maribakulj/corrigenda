"""corrigenda[qe] — the zero-shot D'AlemBERT masked-LM QE scorer.

Heavy and bundle-dependent, so it self-skips when either the
``onnxruntime``/``tokenizers`` extra OR the exported ONNX bundle is
absent (same self-skip discipline as the ``external_corpus`` corpus
tests). The pure ``QEScorer`` protocol + routing wiring is covered
without the extra in ``test_quality.py`` and ``test_routing_pipeline.py``.

These assertions never hard-code a magic score threshold: they check the
ORDERING the doctrine promises (raw OCR needs correction more than its
clean, period-orthography reference), which is invariant to the Platt
calibration constants.
"""

from __future__ import annotations

import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")
pytest.importorskip("numpy")

from corrigenda.core.quality import (  # noqa: E402
    QEScorer,
    RoutingDecision,
    RoutingPolicy,
    route_line,
)
from corrigenda.integrations.qe import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    MaskedLMQEScorer,
    _deglyph,
)

pytestmark = pytest.mark.skipif(
    not (DEFAULT_MODEL_DIR / "model.onnx").exists(),
    reason=(
        "D'AlemBERT ONNX bundle absent; build it with "
        "scripts/export_dalembert_onnx.py --out ~/.cache/corrigenda/dalembert-onnx"
    ),
)

# Clean human reference (period orthography: long-s, u-for-v) vs the real
# raw OCR of the same line (eſt misread as eû).
REF = "qu'il eſt bon de les auoir"
RAW = "qu'il eû bon de les auoir"


@pytest.fixture(scope="module")
def scorer() -> MaskedLMQEScorer:
    return MaskedLMQEScorer()


def test_implements_qescorer_protocol(scorer: MaskedLMQEScorer) -> None:
    assert isinstance(scorer, QEScorer)  # runtime_checkable structural match
    assert scorer.name == "dalembert-qe"


def test_score_is_in_unit_interval(scorer: MaskedLMQEScorer) -> None:
    for text in (REF, RAW, "", "  ,  «» — ", "8 Discours.", "cukiuent"):
        score = scorer.needs_correction(text)
        assert 0.0 <= score <= 1.0, (text, score)


def test_raw_ocr_scores_higher_than_clean_reference(scorer: MaskedLMQEScorer) -> None:
    # The scorer's whole reason to exist: a raw OCR line needs correction
    # MORE than its clean reference — the signal the Phase-2 harness proved
    # the heuristic baseline was missing.
    assert scorer.needs_correction(RAW) > scorer.needs_correction(REF)


def test_archaic_glyphs_are_not_treated_as_errors(scorer: MaskedLMQEScorer) -> None:
    # ROADMAP rule 3: a clean line FULL of long-s / u-for-v must not look
    # like it needs correction merely for its historical glyphs. It must
    # score well below a genuinely broken line of similar length.
    clean_archaic = "Il eſt bon de ſçauoir quelque choſe des meurs"
    broken = "Il efi bon de fçanoir quelq chnfe des rncurs"
    assert scorer.needs_correction(clean_archaic) < scorer.needs_correction(broken)


def test_empty_and_punctuation_only_lines_are_zero(scorer: MaskedLMQEScorer) -> None:
    assert scorer.needs_correction("") == 0.0
    assert scorer.needs_correction("  «» — , ") == 0.0


def test_scoring_reports_original_archaic_words_not_deglyphed(
    scorer: MaskedLMQEScorer,
) -> None:
    # Deglyphing is scoring-only: the per-word report carries the ORIGINAL
    # historical spelling, and the input string is untouched.
    text = "qu'il eſt bon"
    reported = dict(scorer.score_words(text))
    assert "eſt" in reported  # archaic form preserved in the report
    assert "est" not in reported  # never the deglyphed twin
    assert text == "qu'il eſt bon"


def test_deglyph_preserves_word_count_and_order() -> None:
    # The word→subword mapping relies on glyph substitution never adding or
    # removing a whitespace boundary.
    for text in (REF, "des richeſſes & œufs", "ﬁn ﬂeur æques"):
        assert len(_deglyph(text).split()) == len(text.split())


def test_qe_score_drives_router_skip_vs_send(scorer: MaskedLMQEScorer) -> None:
    # End-to-end doctrine: the scorer INFORMS, the Router decides. With a
    # skip bound between the two scores, the clean line is SKIP-ped (no LLM
    # call) and the raw line still goes to the LLM.
    lo = scorer.needs_correction(REF)
    hi = scorer.needs_correction(RAW)
    assert lo < hi
    policy = RoutingPolicy(skip_at_or_below=(lo + hi) / 2)
    assert route_line(lo, policy) is RoutingDecision.SKIP
    assert route_line(hi, policy) is RoutingDecision.LLM


def test_invalid_scale_rejected() -> None:
    with pytest.raises(ValueError):
        MaskedLMQEScorer(surprisal_scale=0.0)
