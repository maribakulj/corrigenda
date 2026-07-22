"""Token-to-token alignment between a source line and its correction.

ROADMAP V3 Phase 1 — the shared component: the same alignment serves
(1) faithful projection in the rewriters' slow path (recycle a word's
identity onto the word it actually corresponds to, never onto whatever
happens to sit at the same position), (2) per-token confidence scoring,
and (3) the future ``token_realign`` loss policy.

Pure stdlib, no dependency: character-level Levenshtein similarity
between tokens drives a dynamic-programming alignment over the two
token sequences. Monotonic by construction (an alignment never crosses);
a suspected word MOVE — a token the alignment could not settle that has
a near-identical counterpart elsewhere — is *flagged*, never acted on:
deciding what to do with a reordering belongs to policies, not here.

Costs: gap (insertion/deletion) = 1.0, substitution = 2 × (1 − sim).
A substitution is therefore chosen over a gap pair only when the tokens
share at least one character of similarity (sim > 0); two tokens with
nothing in common fall to deletion + insertion instead of fabricating a
correspondence — identity must never ride a zero-evidence match.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A deleted/weakly-matched token whose near-identical twin (≥ this
#: similarity) appears elsewhere in the target flags the alignment as a
#: suspected move.
_MOVE_SIMILARITY = 0.8

#: Below this similarity a DP match is considered "weak" — kept in the
#: alignment (it is still the best monotonic reading) but eligible as a
#: move suspect.
_WEAK_MATCH = 0.5


def char_similarity(a: str, b: str) -> float:
    """Character-level similarity in [0, 1]: 1 − levenshtein/max_len.

    ``1.0`` for identical tokens (including two empty strings), ``0.0``
    for tokens sharing nothing.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    # Classic two-row Levenshtein — tokens are words, lengths are tiny.
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (0 if ca == cb else 1),  # substitution
                )
            )
        previous = current
    return 1.0 - previous[-1] / max(len(a), len(b))


@dataclass(frozen=True)
class AlignedPair:
    """One correspondence in the alignment.

    ``source_index is None`` → the target token is an INSERTION;
    ``target_index is None`` → the source token is a DELETION;
    both set → a match, with the pair's character similarity.
    """

    source_index: int | None
    target_index: int | None
    similarity: float


@dataclass(frozen=True)
class TokenAlignment:
    """The full alignment of one line's source tokens onto its correction."""

    pairs: tuple[AlignedPair, ...]
    #: Aggregate in [0, 1]: sum of matched similarities over
    #: ``max(len(source), len(target))`` — 1.0 means identical sequences.
    score: float
    #: Heuristic flag: some token the alignment could not settle has a
    #: near-identical counterpart elsewhere — the correction *may* have
    #: reordered words. A flag to surface, never a licence to reorder.
    move_suspected: bool

    def source_for_target(self, target_index: int) -> int | None:
        """The source token index matched to ``target_index`` (None =
        the target token is an insertion)."""
        for pair in self.pairs:
            if pair.target_index == target_index:
                return pair.source_index
        return None


def align_tokens(source: list[str], target: list[str]) -> TokenAlignment:
    """Align ``source`` tokens onto ``target`` tokens (both in reading
    order). Deterministic; O(len(source) × len(target)) token pairs."""
    n, m = len(source), len(target)

    # sim[i][j] between source[i] and target[j], computed once.
    sim = [[char_similarity(source[i], target[j]) for j in range(m)] for i in range(n)]

    gap = 1.0
    # cost[i][j] = best cost aligning source[:i] with target[:j].
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i * gap
    for j in range(1, m + 1):
        cost[0][j] = j * gap
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(
                cost[i - 1][j] + gap,  # delete source[i-1]
                cost[i][j - 1] + gap,  # insert target[j-1]
                cost[i - 1][j - 1] + 2.0 * (1.0 - sim[i - 1][j - 1]),
            )

    # Backtrack (prefer match > deletion > insertion on exact ties, but a
    # zero-similarity "match" costs exactly gap+gap and must NOT win: a
    # correspondence needs evidence).
    pairs: list[AlignedPair] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            sub = cost[i - 1][j - 1] + 2.0 * (1.0 - sim[i - 1][j - 1])
            if abs(cost[i][j] - sub) < 1e-9 and sim[i - 1][j - 1] > 0.0:
                pairs.append(AlignedPair(i - 1, j - 1, sim[i - 1][j - 1]))
                i, j = i - 1, j - 1
                continue
        if i > 0 and abs(cost[i][j] - (cost[i - 1][j] + gap)) < 1e-9:
            pairs.append(AlignedPair(i - 1, None, 0.0))
            i -= 1
            continue
        pairs.append(AlignedPair(None, j - 1, 0.0))
        j -= 1
    pairs.reverse()

    matched = [p for p in pairs if p.source_index is not None and p.target_index is not None]
    denominator = max(n, m, 1)
    score = sum(p.similarity for p in matched) / denominator

    # Move suspicion: a deleted or weakly-matched source token whose
    # near-identical twin sits elsewhere in the target.
    move = False
    for pair in pairs:
        src = pair.source_index
        if src is None:
            continue
        if pair.target_index is not None and pair.similarity >= _WEAK_MATCH:
            continue  # confidently settled
        for j2 in range(m):
            if j2 != pair.target_index and sim[src][j2] >= _MOVE_SIMILARITY:
                move = True
                break
        if move:
            break

    return TokenAlignment(pairs=tuple(pairs), score=score, move_suspected=move)


__all__ = ["AlignedPair", "TokenAlignment", "align_tokens", "char_similarity"]
