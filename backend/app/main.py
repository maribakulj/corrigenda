"""FastAPI application entry point."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.jobs import router as jobs_router
from app.api.providers import router as providers_router
from app.jobs.store import JobStore

# Resolved once at import time — same process for the lifetime of the container
_STATIC_DIR = Path(__file__).parent.parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="ALTO LLM Corrector",
        description="Post-OCR text correction of ALTO XML files using LLM providers.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Bind infrastructure to app.state for dependency injection.
    # Endpoints reach this through `Depends(get_job_store)` rather than
    # importing a module-level singleton — see app/api/deps.py.
    app.state.job_store = JobStore()

    # ------------------------------------------------------------------
    # CORS
    # Origins are configurable via CORS_ORIGINS env var (comma-separated).
    # Default: wildcard. No credentials — NEVER combine allow_credentials
    # with allow_origins=["*"] (Starlette raises ValueError).
    # ------------------------------------------------------------------
    cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Health check — registered first, always reachable, never depends
    # on static files or any other optional feature.
    # ------------------------------------------------------------------
    @app.get("/health", include_in_schema=False)
    async def health():
        return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # API routers
    # ------------------------------------------------------------------
    app.include_router(providers_router, prefix="/api/providers", tags=["providers"])
    app.include_router(jobs_router, prefix="/api/jobs", tags=["jobs"])

    # ------------------------------------------------------------------
    # Static frontend (HF Spaces single-container mode)
    # Mount /assets for cache-able JS/CSS, then serve index.html for
    # every other path so the React SPA handles its own routing.
    # Routes are ALWAYS registered regardless of whether static files
    # exist — root always returns 200, avoiding health-check failures.
    # ------------------------------------------------------------------
    assets_dir = _STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    async def root():
        if _INDEX_HTML.exists():
            return FileResponse(str(_INDEX_HTML))
        return JSONResponse({"status": "ok"})

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if _INDEX_HTML.exists():
            return FileResponse(str(_INDEX_HTML))
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
