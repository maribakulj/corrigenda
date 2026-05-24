"""FastAPI dependencies that surface infrastructure to endpoints.

Centralising these resolvers means endpoint signatures don't reach for
module-level singletons. Currently the app stores its `JobStore` on
`app.state` at startup; if that ever moves (per-request store, Redis-
backed store, ...), only the resolvers here change.
"""
from __future__ import annotations

from fastapi import Request

from app.protocols import JobStore


def get_job_store(request: Request) -> JobStore:
    """Return the `JobStore` bound to the current FastAPI app."""
    return request.app.state.job_store
