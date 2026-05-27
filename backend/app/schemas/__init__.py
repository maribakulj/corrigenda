"""Backend's schema surface.

Domain models come from the pure ``alto_core.schemas`` package. HTTP
DTOs (request/response payloads, SSE events) live next door in
:mod:`app.schemas.http` — they're server-layer concerns, not domain.

This module re-exports both groups so existing
``from app.schemas import X`` call sites keep working.
"""

from alto_core.schemas import (
    BlockManifest,
    ChunkGranularity,
    ChunkPlan,
    ChunkPlannerConfig,
    ChunkRequest,
    Coords,
    DocumentManifest,
    HyphenRole,
    JobManifest,
    JobStatus,
    JobTrace,
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
    Provider,
)

from app.schemas.http import (
    CreateJobResponse,
    JobStatusResponse,
    ListModelsRequest,
    ListModelsResponse,
    SSEEvent,
)

__all__ = [
    # Domain (alto-core)
    "BlockManifest",
    "ChunkGranularity",
    "ChunkPlan",
    "ChunkPlannerConfig",
    "ChunkRequest",
    "Coords",
    # HTTP DTOs (backend-local)
    "CreateJobResponse",
    "DocumentManifest",
    "HyphenRole",
    "JobManifest",
    "JobStatus",
    "JobStatusResponse",
    "JobTrace",
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
]
