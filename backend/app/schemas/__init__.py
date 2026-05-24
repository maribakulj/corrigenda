"""Backward-compat shim. Implementation lives in :mod:`alto_core.schemas`.

New code should import from `alto_core.schemas` directly. This module exists
so that the existing `from app.schemas import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.schemas import (  # noqa: F401  re-export
    Any,
    BaseModel,
    BlockManifest,
    ChunkGranularity,
    ChunkPlan,
    ChunkPlannerConfig,
    ChunkRequest,
    Coords,
    CreateJobResponse,
    DocumentManifest,
    Enum,
    Field,
    HyphenRole,
    JobManifest,
    JobStatus,
    JobStatusResponse,
    JobTrace,
    LineManifest,
    LineStatus,
    LineTrace,
    ListModelsRequest,
    ListModelsResponse,
    LLMLineInput,
    LLMLineOutput,
    LLMResponse,
    LLMUserPayload,
    ModelInfo,
    PageManifest,
    Provider,
    SSEEvent,
    uuid,
)
