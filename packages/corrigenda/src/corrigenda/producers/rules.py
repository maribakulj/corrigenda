"""Deterministic rules producer (spec §5.3) — the first real span emitter.

A substitution engine that turns a table of literal/regex rules into
``ReplaceSpan`` ops with exact ``RangeAnchor`` offsets. Zero dependencies,
zero network, reproducible to the byte — which is exactly why it doubles as
the protocol's reference-test producer and as a free pre-LLM correction
pass (``ſ→s``, OCR confusions, punctuation).

Each rule may be **lexicon-guarded**: the substitution is emitted only when
it turns the containing word into a known lexicon entry — the guard that
lets a risky confusion like ``rn→m`` fire on ``moderne`` (from ``modeme``)
without corrupting a word where it does not belong.

Overlapping candidate matches are resolved greedily left-to-right (earliest
start wins, longest at a tie), so every emitted span on a line is
non-overlapping and passes the editing module's E2 check untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from corrigenda.core._norm import ncfold
from corrigenda.core.editing import EditScript, RangeAnchor, ReplaceSpan
from corrigenda.core.protocols import ProducerOptions
from corrigenda.core.schemas import CorrectionRequest, Usage


@dataclass(frozen=True)
class SubstitutionRule:
    """One substitution. ``pattern`` is a literal by default, or a regex when
    ``regex=True`` (then ``replacement`` may use backreferences). When
    ``lexicon_guarded`` the rule fires only if the containing word becomes a
    known lexicon entry."""

    pattern: str
    replacement: str
    regex: bool = False
    lexicon_guarded: bool = False
    name: str = ""

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern if self.regex else re.escape(self.pattern))


def default_french_ocr_rules() -> list[SubstitutionRule]:
    """A conservative, lexicon-free starter set for early-modern French OCR.

    Only substitutions safe without a lexicon: the long s ``ſ`` (U+017F) and
    the ``ﬁ``/``ﬂ`` ligatures. Riskier confusions (``rn→m``) need a lexicon
    and are left to the caller."""
    return [
        SubstitutionRule("ſ", "s", name="long_s"),
        SubstitutionRule("ﬁ", "fi", name="fi_ligature"),
        SubstitutionRule("ﬂ", "fl", name="fl_ligature"),
    ]


def _token_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Whitespace-delimited token bounds enclosing ``[start, end)``."""
    ts = start
    while ts > 0 and not text[ts - 1].isspace():
        ts -= 1
    te = end
    while te < len(text) and not text[te].isspace():
        te += 1
    return ts, te


_STRIP = " \t.,;:!?'\"()[]«»—–-"


class RulesProducer:
    """Deterministic ``EditProducer`` (§5.3).

    ``wants_geometry``/``wants_image`` are ``False`` — a text-only producer.
    ``build_edit_script`` is the pure, synchronous core; ``produce`` is the
    §5.1 contract entry point (``Usage`` is always ``None`` — no tokens
    spent). ``requires_full_coverage`` is ``False``: a line without a
    matching rule is simply left unedited, never an error."""

    wants_geometry: bool = False
    wants_image: bool = False
    #: No op for a line == no edit (identity), NOT a degraded response.
    requires_full_coverage: bool = False

    def __init__(
        self,
        rules: list[SubstitutionRule],
        lexicon: set[str] | None = None,
    ) -> None:
        self._rules = list(rules)
        # Normalise the lexicon once through ncfold (NFC + casefold) for
        # guarded matches. Tokens come from the parser's already-NFC text, so
        # a bare .lower() left a decomposed (NFD) lexicon entry unable to
        # match its composed token — a silently missed guarded correction.
        self._lexicon = {ncfold(w) for w in lexicon} if lexicon else set()

    # -- pure core -------------------------------------------------------

    def _candidates(self, text: str) -> list[tuple[int, int, str, bool]]:
        """All (start, end, replacement, guarded) matches from every rule."""
        out: list[tuple[int, int, str, bool]] = []
        for rule in self._rules:
            for m in rule.compiled().finditer(text):
                start, end = m.start(), m.end()
                if start == end:
                    continue  # never emit a zero-width edit
                replacement = (
                    m.expand(rule.replacement) if rule.regex else rule.replacement
                )
                if rule.lexicon_guarded and not self._word_ok(
                    text, start, end, replacement
                ):
                    continue
                if text[start:end] == replacement:
                    continue  # no-op substitution
                out.append((start, end, replacement, rule.lexicon_guarded))
        return out

    def _word_ok(self, text: str, start: int, end: int, replacement: str) -> bool:
        ts, te = _token_bounds(text, start, end)
        new_token = text[ts:start] + replacement + text[end:te]
        return ncfold(new_token.strip(_STRIP)) in self._lexicon

    def _spans_for_line(self, text: str) -> list[tuple[int, int, str]]:
        """Greedy non-overlapping selection: earliest start, longest at a tie."""
        cands = sorted(self._candidates(text), key=lambda c: (c[0], -(c[1] - c[0])))
        chosen: list[tuple[int, int, str, bool]] = []
        cursor = 0
        for start, end, repl, guarded in cands:
            if start < cursor:
                continue  # overlaps an already-chosen span
            chosen.append((start, end, repl, guarded))
            cursor = end
        return self._validate_composed_tokens(text, chosen)

    def _validate_composed_tokens(
        self,
        text: str,
        chosen: list[tuple[int, int, str, bool]],
    ) -> list[tuple[int, int, str]]:
        """Re-validate tokens carrying SEVERAL composed edits.

        ``_word_ok`` vets each guarded edit against the ORIGINAL token in
        isolation, so two individually-valid edits composing inside one
        whitespace-delimited token can produce a word NOT in the lexicon
        (e.g. 'cornae' + rn→m + ae→a: 'comae' and 'corna' both pass, the
        composed 'coma' does not). When a multi-edit token contains at
        least one guarded edit, the token with ALL its edits applied is
        re-checked against the lexicon; on failure the whole batch for
        that token is rejected (conservative-on-ambiguity — emitting a
        subset would silently change which correction wins).
        """
        by_token: dict[tuple[int, int], list[tuple[int, int, str, bool]]] = {}
        for span in chosen:
            bounds = _token_bounds(text, span[0], span[1])
            by_token.setdefault(bounds, []).append(span)

        rejected: set[tuple[int, int]] = set()
        for (ts, te), spans in by_token.items():
            if len(spans) < 2 or not any(guarded for *_x, guarded in spans):
                continue  # single edits keep their historical validation
            composed = ""
            cursor = ts
            for start, end, repl, _guarded in spans:  # already start-ordered
                composed += text[cursor:start] + repl
                cursor = end
            composed += text[cursor:te]
            if ncfold(composed.strip(_STRIP)) not in self._lexicon:
                rejected.add((ts, te))

        return [
            (start, end, repl)
            for start, end, repl, _guarded in chosen
            if _token_bounds(text, start, end) not in rejected
        ]

    def build_edit_script(
        self,
        canonical_by_id: dict[str, str],
        target_ids: set[str] | None = None,
    ) -> EditScript:
        """Build a deterministic ``EditScript`` for the given line texts."""
        ops: list[ReplaceSpan] = []
        for line_id, text in canonical_by_id.items():
            if target_ids is not None and line_id not in target_ids:
                continue
            for start, end, repl in self._spans_for_line(text):
                ops.append(
                    ReplaceSpan(
                        line_id=line_id,
                        anchor=RangeAnchor(start=start, end=end),
                        text=repl,
                    )
                )
        return EditScript(ops=ops)  # type: ignore[arg-type]

    # -- EditProducer contract (§5.1) ------------------------------------

    async def produce(
        self, payload: CorrectionRequest, *, options: ProducerOptions
    ) -> tuple[EditScript, Usage | None]:
        """§5.1 entry point — deterministic, so ``policy`` is unused and
        ``Usage`` is ``None`` (no tokens spent). Rules run over every line
        in the payload; the pipeline discards ops for context lines (F8)."""
        canonical = {ln.line_id: ln.ocr_text for ln in payload.lines}
        return self.build_edit_script(canonical), None


__all__ = [
    "SubstitutionRule",
    "RulesProducer",
    "default_french_ocr_rules",
]
