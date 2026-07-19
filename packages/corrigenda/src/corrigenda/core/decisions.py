"""Immutable decision record of a run (ADR-011, slices C+E).

The engine expresses its decisions by mutating its PRIVATE working copy
of the manifests (since slice E the caller's document is never
touched); this module defines THE decision model and materializes it
exactly once — after the global consistency pass, when every line's
decision is final. Everything downstream of the run reads the
:class:`DecisionSet`: the projection invariant, fallback accounting,
the final EditScript, and — via :attr:`CorrectionResult.decisions` —
the caller itself.

Materialization enforces terminality: a ``PENDING`` line at this point
is an engine bug — a decision path that forgot its lines — never an
input problem, so the set refuses to exist and the run fails loudly
(the run-level backstop that previously sat beside the write path).

Pure core: no lxml, no formats import (import-contract test).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property

from corrigenda.core.identity import LineRef, line_ref
from corrigenda.core.schemas import DocumentManifest, LineStatus, LineTrace


@dataclass(frozen=True)
class LineDecision:
    """One line's terminal decision — the text the artefact must carry."""

    ref: LineRef
    source_text: str
    final_text: str
    #: Terminal by construction: ``CORRECTED`` or ``FALLBACK``.
    status: LineStatus
    #: The trace's fallback reason for a fallen line (``None`` on
    #: corrected lines, or when the host ran without traces).
    fallback_reason: str | None


@dataclass(frozen=True)
class DecisionSet:
    """Every line's terminal decision, in document reading order."""

    decisions: tuple[LineDecision, ...]

    @cached_property
    def by_ref(self) -> dict[LineRef, LineDecision]:
        """Index: qualified line identity → its decision."""
        return {d.ref: d for d in self.decisions}

    @property
    def fallback_lines(self) -> int:
        """Lines whose terminal status is ``FALLBACK`` (they kept their
        OCR source text, whatever path led there)."""
        return sum(1 for d in self.decisions if d.status is LineStatus.FALLBACK)

    def fallback_reason_counts(self) -> dict[str, int]:
        """Fallen lines aggregated by reason PREFIX (the part before
        ``:``; ``unspecified`` when no trace pinned one) — so a consumer
        can say WHY without parsing messages."""
        counts: dict[str, int] = {}
        for d in self.decisions:
            if d.status is not LineStatus.FALLBACK:
                continue
            prefix = (d.fallback_reason or "unspecified").split(":", 1)[0].strip()
            counts[prefix] = counts.get(prefix, 0) + 1
        return counts


def derive_decision_set(
    document_manifest: DocumentManifest,
    traces: Mapping[LineRef, LineTrace],
) -> DecisionSet:
    """Materialize the run's decisions from the run's manifest copy.

    Called once, after the global consistency pass — the point where no
    later pass may change a decision. Refuses a ``PENDING`` line: an
    undecided line reaching materialization is an engine bug and must
    fail the run before any output exists.
    """
    undecided = [
        (page.page_id, lm.line_id)
        for page in document_manifest.pages
        for lm in page.lines
        if lm.status is LineStatus.PENDING
    ]
    if undecided:
        shown = ", ".join(f"({p!r}, {li!r})" for p, li in undecided[:5])
        suffix = " …" if len(undecided) > 5 else ""
        raise RuntimeError(
            f"{len(undecided)} line(s) reached the end of the run with no "
            f"terminal decision (PENDING): {shown}{suffix}"
        )

    decisions: list[LineDecision] = []
    for page in document_manifest.pages:
        for lm in page.lines:
            ref = line_ref(lm)
            reason: str | None = None
            if lm.status is LineStatus.FALLBACK:
                trace = traces.get(ref)
                reason = trace.fallback_reason if trace is not None else None
            decisions.append(
                LineDecision(
                    ref=ref,
                    source_text=lm.ocr_text,
                    final_text=(
                        lm.corrected_text
                        if lm.corrected_text is not None
                        else lm.ocr_text
                    ),
                    status=lm.status,
                    fallback_reason=reason,
                )
            )
    return DecisionSet(decisions=tuple(decisions))


__all__ = [
    "DecisionSet",
    "LineDecision",
    "derive_decision_set",
]
