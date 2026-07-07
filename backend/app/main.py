"""FastAPI application entry point."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.providers import router as providers_router
from app.api.rate_limit import limiter
from app.jobs.store import JobStore
from app.jobs.task_registry import BackgroundTaskRegistry
from app.observability.logging_config import setup_json_logging

# Resolved once at import time — same process for the lifetime of the container
_STATIC_DIR = Path(__file__).parent.parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks.

    On startup: nothing extra (state is set up in ``create_app``).
    On shutdown: ask the background-task registry to drain in-flight
    correction jobs so we don't leave half-written output files when
    Docker/HF Spaces sends SIGTERM during a redeploy.
    """
    yield
    registry: BackgroundTaskRegistry | None = getattr(app.state, "tasks", None)
    if registry is not None:
        await registry.shutdown(timeout=30.0)


def _rate_limit_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render slowapi's RateLimitExceeded as a uniform JSON 429.

    Signature widened to ``(Request, Exception) -> JSONResponse`` so the
    handler conforms to Starlette's ``add_exception_handler`` type
    contract. SlowAPI only ever dispatches this handler for
    ``RateLimitExceeded`` at runtime (it's registered against that
    type), so the isinstance narrowing is a guaranteed match — the
    fallback string defends against future misregistration only.
    """
    detail = exc.detail if isinstance(exc, RateLimitExceeded) else "rate limit"
    return JSONResponse(
        {"detail": f"Rate limit exceeded: {detail}"},
        status_code=429,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    # Configure root logging first so every subsequent log line (FastAPI's
    # startup, our endpoints, corrigenda's emitted events via LoggingObserver)
    # goes through the JSON formatter. Idempotent — safe to call on every
    # create_app (tests instantiate the app many times).
    setup_json_logging()

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
    # Strong-referenced registry for fire-and-forget background tasks
    # (correction runs spawned from POST /api/jobs). Drained on
    # shutdown by the lifespan handler above.
    app.state.tasks = BackgroundTaskRegistry()

    # ------------------------------------------------------------------
    # Middleware stack (Starlette applies LIFO, so the LAST middleware
    # added wraps the OUTSIDE of the request flow). Target request flow:
    #
    #     incoming → CORS → ProxyHeaders → SlowAPI → endpoint
    #
    # so we add them in reverse:
    #   1. SlowAPI (innermost) — sees the real client IP set by step 2.
    #   2. ProxyHeaders (middle) — rewrites request.client.host from
    #      X-Forwarded-For when the upstream is in TRUSTED_PROXIES.
    #   3. CORS (outermost) — answers OPTIONS preflights directly,
    #      short-circuiting the rest of the stack.
    #
    # Note (R2): keeping CORS *outside* SlowAPI means OPTIONS preflights
    # bypass the rate limiter. This is deliberate: rate-limiting
    # preflights would surface as opaque CORS errors in the browser
    # the moment a user clicks faster than the cap. Preflights are
    # cheap (no body, no DB, no LLM call), so the cost of not
    # counting them is negligible compared to the UX cost of doing so.
    # ------------------------------------------------------------------

    # 1. SlowAPI (innermost) — per-IP rate limiting.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)

    # 2. ProxyHeaders (middle) — translate X-Forwarded-For into
    # request.client.host so slowapi keys on the real caller IP.
    # TRUSTED_PROXIES (comma-separated host list, "*" = trust any
    # upstream) must be set to the proxy IP in production deployments;
    # default 127.0.0.1 keeps a dev-safe stance (no spoofing from
    # outside the loopback). HF Spaces / k8s Dockerfiles override it
    # to "*" because the platform's edge proxy strips and re-emits
    # X-Forwarded-For — apps behind it can trust the header.
    trusted_proxies_raw = os.environ.get("TRUSTED_PROXIES", "127.0.0.1")
    trusted_proxies = [h.strip() for h in trusted_proxies_raw.split(",") if h.strip()]
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxies)

    # 3. CORS (outermost) — origins configurable via CORS_ORIGINS env
    # var (comma-separated). Default: wildcard. No credentials —
    # NEVER combine allow_credentials with allow_origins=["*"]
    # (Starlette raises ValueError).
    cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Health checks — registered first, always reachable. /health stays
    # as the lightweight legacy ping (HF Spaces hits it); /health/live
    # and /health/ready are the new explicit probes (see app/api/health.py).
    # ------------------------------------------------------------------
    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.include_router(health_router, tags=["health"])

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
