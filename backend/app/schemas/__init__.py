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
    DocumentManifest,
    HyphenRole,
    LineManifest,
    LineStatus,
    LineTrace,
    LLMLineInput,
    LLMLineOutput,
    LLMResponse,
    LLMUserPayload,
    ModelInfo,
    PageManifest,
    PipelineEventType,
    Usage,
)

from app.schemas.http import (
    CreateJobResponse,
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
    # HTTP DTOs (backend-local)
    "CreateJobResponse",
    "DocumentManifest",
    "HyphenRole",
    "JobManifest",
    "JobStatus",
    "JobStatusResponse",
    "LLMLineInput",
    "LLMLineOutput",
    "LLMResponse",
    "LLMUserPayload",
    "LineManifest",
    "LineStatus",
    "LineTrace",
    "ListModelsRequest",
    "ListModelsResponse",
    "ModelInfo",
    "PageManifest",
    "PipelineEventType",
    "Provider",
    "SSEEvent",
    "Usage",
]
