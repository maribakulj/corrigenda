"""HTTP-layer DTOs.

These Pydantic models describe request/response payloads exchanged with
the FastAPI server and the SSE stream. They are intentionally NOT in
``corrigenda.core.schemas`` because the pure correction pipeline doesn't
speak HTTP — only the server does. See ARCHITECTURE.md §3.2.

When the eventual `alto-server` package is extracted (Phase 3), this
file moves there verbatim.
"""

from __future__ import annotations

from typing import Any

from corrigenda.core.schemas import ModelInfo, PipelineEventType
from pydantic import BaseModel, Field

from app.schemas.job import JobStatus, Provider


class ListModelsRequest(BaseModel):
    provider: Provider
    api_key: str


class ListModelsResponse(BaseModel):
    provider: Provider
    models: list[ModelInfo]


class CreateJobResponse(BaseModel):
    job_id: str
    # P1-7 — capability token, shown ONCE at creation. Only its SHA-256
    # hash is stored server-side; every subsequent job endpoint requires
    # it (X-Job-Token header, or ?token= for EventSource/img/download
    # links that cannot carry headers).
    job_token: str | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_lines: int = 0
    lines_modified: int = 0
    chunks_total: int = 0
    retries: int = 0
    fallbacks: int = 0
    duration_seconds: float | None = None
    error: str | None = None


class SSEEvent(BaseModel):
    # PipelineEventType is `str, Enum` — accepts both enum members
    # (emitter side, type-checked) and bare strings (wire format,
    # frontend consumer). Coercion is lossless because the enum's
    # serialised value is its string member.
    event: PipelineEventType | str
    data: dict[str, Any] = Field(default_factory=dict)
