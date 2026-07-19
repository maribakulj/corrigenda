"""Backend's schema surface.

Domain models come from the pure ``corrigenda.core.schemas`` package. HTTP
DTOs (request/response payloads, SSE events) live next door in
:mod:`app.schemas.http` — they're server-layer concerns, not domain.

This module re-exports both groups so existing
``from app.schemas import X`` call sites keep working.
"""

from corrigenda.core.schemas import (
    BlockManifest,
    ChunkGranularity,
    ChunkPlan,
    ChunkPlannerConfig,
    ChunkRequest,
    Coords,
    CorrectionReport,
    CorrectionRequest,
    DocumentManifest,
    HyphenRole,
    LineContext,
    LineManifest,
    LineOutcome,
    LineProposal,
    LineStatus,
    LineTrace,
    ModelInfo,
    PageManifest,
    PipelineEventType,
    ProposalBatch,
    Usage,
)

from app.schemas.http import (
    CreateJobResponse,
    DownloadUrlResponse,
    EventsUrlResponse,
    JobStatusResponse,
    ListModelsRequest,
    ListModelsResponse,
    SSEEvent,
)

# Server-side job enums + record — moved out of corrigenda by spec F12.
from app.schemas.job import (
    TERMINAL_SUCCESS_STATES,
    JobManifest,
    JobStatus,
    Provider,
)

__all__ = [
    "TERMINAL_SUCCESS_STATES",
    # Domain (corrigenda)
    "BlockManifest",
    "ChunkGranularity",
    "ChunkPlan",
    "ChunkPlannerConfig",
    "ChunkRequest",
    "Coords",
    "CorrectionReport",
    "CorrectionRequest",
    # HTTP DTOs (backend-local)
    "CreateJobResponse",
    "DocumentManifest",
    "DownloadUrlResponse",
    "EventsUrlResponse",
    "HyphenRole",
    "JobManifest",
    "JobStatus",
    "JobStatusResponse",
    "LineContext",
    "LineManifest",
    "LineOutcome",
    "LineProposal",
    "LineStatus",
    "LineTrace",
    "ListModelsRequest",
    "ListModelsResponse",
    "ModelInfo",
    "PageManifest",
    "PipelineEventType",
    "ProposalBatch",
    "Provider",
    "SSEEvent",
    "Usage",
]
