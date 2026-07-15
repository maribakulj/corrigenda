"""Health endpoints.

Two checks, deliberately separated:

- ``GET /health/live`` — process is alive and the event loop is
  responding. Used by k8s/Docker liveness probes (or by HF Spaces
  if we ever opt into them). Always 200; the only way this fails is
  the process being unable to schedule the handler at all.

- ``GET /health/ready`` — the process is ready to accept work: the
  JobStore is reachable and the job storage directory is writable.
  Returns 503 with a JSON body listing what's broken if any check
  fails. Used to gate traffic during startup or when the disk
  fills up.

These are intentionally NOT part of the main API (``/api/...``)
because operators want to hit them without going through any auth or
rate-limit middleware that may sit in front of the app.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_job_store
from app.frontend_static import INDEX_HTML, frontend_expected
from app.protocols import JobStore

router = APIRouter()


@router.get("/health/live", include_in_schema=False)
async def live() -> JSONResponse:
    """Liveness probe — process is responsive."""
    return JSONResponse({"status": "ok"})


def _storage_writable(path: Path) -> bool:
    """True iff ``path`` is an existing directory we have write access to.

    Observation-only: never creates, writes to, or unlinks anything.
    A readiness probe that mutates the filesystem (a) silently masks a
    genuine "storage dir missing" bug by provisioning it, and (b) is
    non-idempotent under high probe frequency.
    """
    return path.is_dir() and os.access(path, os.W_OK)


@router.get("/health/ready", include_in_schema=False)
async def ready(
    request: Request,
    store: JobStore = Depends(get_job_store),
) -> JSONResponse:
    """Readiness probe — JobStore reachable AND job dir writable."""
    checks: dict[str, str] = {}

    # JobStore must respond to a trivial query. ``get_job`` of a
    # non-existent id should return None, never raise.
    try:
        store.get_job("__readiness_probe__")
        checks["job_store"] = "ok"
    except Exception as exc:
        checks["job_store"] = f"error: {type(exc).__name__}"

    # Storage directory check runs off the event loop: even a single
    # `os.access` syscall can stall asyncio for tens of milliseconds on
    # a slow filesystem (NFS, container overlay under pressure), which
    # in turn freezes every concurrent SSE stream the server is serving.
    storage_dir = Path(os.environ.get("JOB_STORAGE_DIR", "/tmp/app-jobs"))
    try:
        is_writable = await asyncio.to_thread(_storage_writable, storage_dir)
    except Exception as exc:
        checks["storage_dir"] = f"error: {type(exc).__name__}"
    else:
        checks["storage_dir"] = "ok" if is_writable else "error: not writable"

    # Plan V1.3 — a deployment that PROMISES the SPA (SERVE_FRONTEND=1)
    # but lacks the built index.html is not ready: the historical
    # wrong-COPY regression kept /health green while the frontend was
    # gone. Backend-only deployments (variable unset) skip the check.
    if frontend_expected():
        exists = await asyncio.to_thread(INDEX_HTML.exists)
        checks["frontend"] = "ok" if exists else "error: index.html missing"

    healthy = all(v == "ok" for v in checks.values())
    # Plan V2.1 — separate load gauges: uploads being staged vs
    # pipelines running. Informational (never flips readiness); lets
    # operators see which capacity pool is saturated.
    load = {
        "uploads_in_progress": getattr(request.app.state, "uploads_in_progress", 0),
        "jobs_running": request.app.state.tasks.active_count,
    }
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks, "load": load},
        status_code=200 if healthy else 503,
    )
