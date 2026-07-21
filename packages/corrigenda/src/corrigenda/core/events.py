"""Typed engine events (P3.6, second slice).

Each dataclass here is THE definition of one
:class:`~corrigenda.core.schemas.PipelineEventType`'s payload — the
emit sites in the pipeline construct these instead of ad-hoc dict
literals, so an event's shape lives in exactly one place and the
bijection type↔class is testable. The observer port keeps its wire
shape (``on_event(event_type, payload)``): the pipeline renders
``event.type`` + ``event.payload()`` at the boundary, so existing
observers (the backend's SSE fan-out, every test double) keep working
unchanged and the SSE wire format is byte-identical.

Pure core: no lxml, no I/O (import-contract test).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from corrigenda.core.schemas import PipelineEventType


@dataclass(frozen=True)
class EngineEvent:
    """Base of every typed pipeline event."""

    #: The wire event name this class defines the payload of.
    type: ClassVar[PipelineEventType]

    def payload(self) -> dict[str, Any]:
        """The event's wire payload — exactly this dataclass's fields."""
        return asdict(self)


@dataclass(frozen=True)
class DocumentParsed(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.DOCUMENT_PARSED

    total_pages: int
    total_lines: int
    hyphen_pairs: int


@dataclass(frozen=True)
class PageStarted(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.PAGE_STARTED

    page_id: str
    page_index: int
    line_count: int
    hyphen_pair_count: int


@dataclass(frozen=True)
class PageCompleted(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.PAGE_COMPLETED

    page_id: str
    page_index: int
    corrections: int
    hyphen_pairs_reconciled: int


@dataclass(frozen=True)
class ChunkPlanned(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.CHUNK_PLANNED

    page_id: str
    chunk_count: int
    granularity: str


@dataclass(frozen=True)
class ChunkStarted(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.CHUNK_STARTED

    chunk_id: str
    granularity: str
    line_count: int


@dataclass(frozen=True)
class ChunkCompleted(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.CHUNK_COMPLETED

    chunk_id: str
    line_count: int
    target_count: int
    hyphen_pairs_reconciled: int
    # F14 — token usage for this chunk's producer call (0 when the
    # provider did not report it).
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ChunkError(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.CHUNK_ERROR

    chunk_id: str
    message: str
    exception_type: str


@dataclass(frozen=True)
class ChunkDowngraded(EngineEvent):
    """F1 — the chunk's retry budget is exhausted; its target lines are
    re-planned at the next-finer granularity."""

    type: ClassVar[PipelineEventType] = PipelineEventType.CHUNK_DOWNGRADED

    chunk_id: str
    from_granularity: str
    to_granularity: str
    line_count: int
    target_count: int
    budget_remaining: int


@dataclass(frozen=True)
class Retry(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.RETRY

    chunk_id: str
    attempt: int
    error: str


@dataclass(frozen=True)
class Warning(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.WARNING

    chunk_id: str
    message: str


@dataclass(frozen=True)
class HyphenPartnerMissing(EngineEvent):
    type: ClassVar[PipelineEventType] = PipelineEventType.HYPHEN_PARTNER_MISSING

    chunk_id: str
    line_id: str
    missing_partner_id: str
    direction: str  # "forward" | "backward"


@dataclass(frozen=True)
class RewriterStats(EngineEvent):
    """Per-file rewriter path counts — pure read-only diagnostics."""

    type: ClassVar[PipelineEventType] = PipelineEventType.REWRITER_STATS

    source_stem: str
    untouched: int
    subs_only: int
    fast_path: int
    slow_path: int


@dataclass(frozen=True)
class ReconcileStats(EngineEvent):
    """Run-level reconciliation counts. Engine vocabulary, emitted by
    the HOST at job end (from ``result.reconcile_metrics``) so
    subscribers that exit on the terminal event still receive it."""

    type: ClassVar[PipelineEventType] = PipelineEventType.RECONCILE_STATS

    coherent: int
    fallback: int
    neutralised: int
    total: int


#: type → its one payload class; the bijection is pinned by
#: ``tests/test_engine_events.py``.
EVENT_CLASSES: tuple[type[EngineEvent], ...] = (
    DocumentParsed,
    PageStarted,
    PageCompleted,
    ChunkPlanned,
    ChunkStarted,
    ChunkCompleted,
    ChunkError,
    ChunkDowngraded,
    Retry,
    Warning,
    HyphenPartnerMissing,
    RewriterStats,
    ReconcileStats,
)


__all__ = [
    "EngineEvent",
    "DocumentParsed",
    "PageStarted",
    "PageCompleted",
    "ChunkPlanned",
    "ChunkStarted",
    "ChunkCompleted",
    "ChunkError",
    "ChunkDowngraded",
    "Retry",
    "Warning",
    "HyphenPartnerMissing",
    "RewriterStats",
    "ReconcileStats",
    "EVENT_CLASSES",
]
