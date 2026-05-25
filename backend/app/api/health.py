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

import os
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_job_store
from app.protocols import JobStore

router = APIRouter()


@router.get("/health/live", include_in_schema=False)
async def live() -> JSONResponse:
    """Liveness probe — process is responsive."""
    return JSONResponse({"status": "ok"})


@router.get("/health/ready", include_in_schema=False)
async def ready(
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

    # Storage directory must exist and be writable.
    storage_dir = Path(os.environ.get("JOB_STORAGE_DIR", "/tmp/app-jobs"))
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        probe = storage_dir / ".readiness_probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        checks["storage_dir"] = "ok"
    except Exception as exc:
        checks["storage_dir"] = f"error: {type(exc).__name__}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )
