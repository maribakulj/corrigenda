"""Backend's schema surface.

Domain models come from the pure ``alto_core.schemas`` package. HTTP
DTOs (request/response payloads, SSE events) live next door in
:mod:`app.schemas.http` — they're server-layer concerns, not domain.

This module re-exports both groups so existing
``from app.schemas import X`` call sites keep working.
"""

from alto_core.schemas import (  # noqa: F401  re-export — domain
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
    Provider,
)

from app.schemas.http import (  # noqa: F401  re-export — HTTP DTOs
    CreateJobResponse,
    JobStatusResponse,
    ListModelsRequest,
    ListModelsResponse,
    SSEEvent,
)
