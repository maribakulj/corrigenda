"""Confidence scoring for line decisions (ROADMAP V3 Phase 1).

The confidence of a line is MULTI-COMPONENT and every component keeps
its own name (:class:`~corrigenda.core.schemas.LineConfidence`): the
source OCR confidence, the producer's self-assessment (fed by the LLM
uncertainty channel once it lands), the token-alignment score, and any
number of injectable :class:`ConfidenceScorer` implementations. The
aggregate ``decision`` value uses an IDENTIFIED formula — never a magic
number whose recipe is lost.

Doctrine: these scores order lines from safest to riskiest (review
queues, routing). They are NOT calibrated probabilities until the
Phase 2 harness measures them against a real corpus — which is also
why ``ConfidencePolicy.write_wc`` stays locked.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from corrigenda.core.alignment import TokenAlignment, align_tokens, char_similarity
from corrigenda.core.schemas import LineConfidence

#: Classic OCR confusions for heritage French print. A changed token
#: whose diff is exactly one of these substitutions is a HIGH-confidence
#: correction regardless of raw character distance — the pattern is the
#: evidence. Injectable on :class:`HeuristicScorer` for other domains.
DEFAULT_CONFUSIONS: tuple[tuple[str, str], ...] = (
    ("ſ", "s"),
    ("ﬁ", "fi"),
    ("ﬂ", "fl"),
    ("rn", "m"),
    ("m", "rn"),
    ("u", "n"),
    ("n", "u"),
    ("1", "l"),
    ("l", "1"),
    ("0", "o"),
    ("æ", "ae"),
    ("œ", "oe"),
)

_CONFUSION_SCORE = 0.95
_LEXICON_SCORE = 0.9
_INSERTION_SCORE = 0.3
_DELETION_SCORE = 0.4

#: Claim-verification values (uncertainty channel). Starting points for
#: the Phase 2 calibration, like every constant in this module.
_FAILED_CLAIM_SCORE = 0.2
_CONTEXT_CLAIM_SCORE = 0.6
_CONJECTURE_SCORE = 0.3
_UNCERTAIN_STATUS_SCORE = 0.3
_BARE_CERTAIN_SCORE = 0.75


def is_known_confusion(
    source: str,
    target: str,
    confusions: tuple[tuple[str, str], ...] = DEFAULT_CONFUSIONS,
) -> bool:
    """True when ONE substitution from the table turns ``source`` into
    ``target`` (checked at every occurrence position)."""
    for old, new in confusions:
        start = 0
        while (i := source.find(old, start)) != -1:
            if source[:i] + new + source[i + len(old) :] == target:
                return True
            start = i + 1
    return False


@runtime_checkable
class ConfidenceScorer(Protocol):
    """A named, deterministic scorer of one line's decision.

    ``score_line`` returns a value in [0, 1] — higher is safer. The
    pipeline records each scorer's value under its ``name`` in
    ``LineConfidence.scorers``; scorers never decide anything, they
    inform (the app decides — same doctrine as producers).
    """

    name: str

    def score_line(
        self,
        *,
        source_text: str,
        final_text: str,
        alignment: TokenAlignment,
    ) -> float: ...


class HeuristicScorer:
    """Zero-dependency scorer: character evidence + known confusion
    patterns + optional lexicon.

    Per CHANGED token event (a matched pair whose text differs, an
    insertion, a deletion), the score is:

    - matched pair — the best of: character similarity; 0.95 when the
      diff is exactly one known confusion substitution; 0.9 when the
      corrected token is a lexicon word and similarity ≥ 0.5;
    - insertion — 0.3 (a word with no source evidence);
    - deletion — 0.4 (source material removed).

    The line score is the mean over events (1.0 when nothing changed).
    Deterministic and cheap by design; its thresholds are starting
    points for the Phase 2 calibration, not truths.
    """

    name: str = "heuristic"

    def __init__(
        self,
        confusions: tuple[tuple[str, str], ...] = DEFAULT_CONFUSIONS,
        lexicon: set[str] | None = None,
    ) -> None:
        self._confusions = confusions
        self._lexicon = {w.casefold() for w in lexicon} if lexicon else set()

    def _token_score(self, source: str, target: str) -> float:
        score = char_similarity(source, target)
        if is_known_confusion(source, target, self._confusions):
            score = max(score, _CONFUSION_SCORE)
        if self._lexicon and target.casefold() in self._lexicon and score >= 0.5:
            score = max(score, _LEXICON_SCORE)
        return score

    def score_line(
        self,
        *,
        source_text: str,
        final_text: str,
        alignment: TokenAlignment,
    ) -> float:
        if source_text == final_text:
            return 1.0
        source_tokens = source_text.split()
        target_tokens = final_text.split()
        events: list[float] = []
        for pair in alignment.pairs:
            if pair.source_index is None:
                events.append(_INSERTION_SCORE)
            elif pair.target_index is None:
                events.append(_DELETION_SCORE)
            else:
                src = source_tokens[pair.source_index]
                tgt = target_tokens[pair.target_index]
                if src != tgt:
                    events.append(self._token_score(src, tgt))
        if not events:
            return 1.0  # e.g. whitespace-only difference
        return sum(events) / len(events)


def score_producer_claims(
    *,
    source_text: str,
    corrected_text: str,
    status: str | None,
    claims: list[dict[str, str]],
    confusions: tuple[tuple[str, str], ...] = DEFAULT_CONFUSIONS,
    lexicon: set[str] | None = None,
) -> float | None:
    """Turn the LLM uncertainty channel's CLAIMS into a verified score.

    Doctrine: the model supplies auditable evidence, never a raw score —
    every verifiable claim is CHECKED, and a claim that fails its check
    scores *below* a plain admission of guessing (a fabricated
    justification is worse evidence than honesty):

    - ``status == "uncertain"`` → 0.3, full stop (claims cannot rescue
      an admitted doubt);
    - ``confusion_connue`` → 0.95 verified against the table, 0.2 when
      the diff is NOT a tabled confusion;
    - ``mot_du_lexique`` → 0.9 when the corrected token is in the
      lexicon, 0.2 otherwise (0.2 too when no lexicon was configured —
      an unverifiable verifiable-class claim);
    - ``infere_du_contexte`` → 0.6 (honest but unverifiable);
    - ``conjecture`` → 0.3;
    - any claim whose ``source``/``corrected`` tokens do not appear in
      the line's actual source/corrected text → 0.2 (fabricated);
    - line score = min over claim scores; ``certain`` with no claims →
      0.75 (a bare self-report, the classic miscalibrated signal).

    Returns ``None`` when the model declared nothing (no status).
    """
    if status is None:
        return None
    if status == "uncertain":
        return _UNCERTAIN_STATUS_SCORE
    if not claims:
        return _BARE_CERTAIN_SCORE

    source_tokens = set(source_text.split())
    corrected_tokens = set(corrected_text.split())
    lex = {w.casefold() for w in lexicon} if lexicon else set()

    scores: list[float] = []
    for claim in claims:
        src = claim.get("source", "")
        tgt = claim.get("corrected", "")
        reason = claim.get("reason", "")
        if src not in source_tokens or tgt not in corrected_tokens:
            scores.append(_FAILED_CLAIM_SCORE)  # fabricated tokens
            continue
        if reason == "confusion_connue":
            scores.append(
                _CONFUSION_SCORE
                if is_known_confusion(src, tgt, confusions)
                else _FAILED_CLAIM_SCORE
            )
        elif reason == "mot_du_lexique":
            scores.append(
                _LEXICON_SCORE if tgt.casefold() in lex else _FAILED_CLAIM_SCORE
            )
        elif reason == "infere_du_contexte":
            scores.append(_CONTEXT_CLAIM_SCORE)
        elif reason == "conjecture":
            scores.append(_CONJECTURE_SCORE)
        else:
            scores.append(_FAILED_CLAIM_SCORE)  # unknown reason code
    return min(scores)


def build_line_confidence(
    *,
    source_text: str,
    final_text: str,
    ocr_confidence: float | None,
    producer_confidence: float | None = None,
    scorers: tuple[ConfidenceScorer, ...] = (),
) -> LineConfidence:
    """Assemble one line's multi-component confidence.

    ``alignment`` is 1.0 for an unchanged line (the written text IS the
    source) and the token-alignment score otherwise. The aggregate is
    ``min`` over every PRESENT component — conservative: a decision is
    only as safe as its weakest evidence. The formula name is recorded
    so a consumer never has to guess the recipe.
    """
    alignment = align_tokens(source_text.split(), final_text.split())
    alignment_score = 1.0 if source_text == final_text else alignment.score

    scorer_values = {
        scorer.name: scorer.score_line(
            source_text=source_text, final_text=final_text, alignment=alignment
        )
        for scorer in scorers
    }

    present: list[float] = [alignment_score, *scorer_values.values()]
    if ocr_confidence is not None:
        present.append(ocr_confidence)
    if producer_confidence is not None:
        present.append(producer_confidence)

    return LineConfidence(
        ocr=ocr_confidence,
        producer=producer_confidence,
        alignment=alignment_score,
        scorers=scorer_values,
        decision=min(present),
        formula="min",
    )


__all__ = [
    "DEFAULT_CONFUSIONS",
    "ConfidenceScorer",
    "HeuristicScorer",
    "build_line_confidence",
    "is_known_confusion",
    "score_producer_claims",
]
