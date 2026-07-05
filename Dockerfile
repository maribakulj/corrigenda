# Stage 1 — Build React frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json .
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2 — Python backend + static frontend
FROM python:3.11-slim
WORKDIR /app

# Two-step Python install:
#   1. corrigenda (sibling package) — editable, absolute path so cwd
#      doesn't matter.
#   2. backend requirements.txt — corrigenda no longer lives in there
#      since Stage 6 of the audit remediation (decoupled to avoid the
#      cwd-relative `-e ../packages/corrigenda` failure mode).
COPY packages/corrigenda /app/packages/corrigenda
RUN pip install --no-cache-dir -e /app/packages/corrigenda

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/app/ /app/backend/app/
# Destination must match ``_STATIC_DIR`` in ``backend/app/main.py``:
# ``Path(__file__).parent.parent / "static"`` resolves to
# ``/app/backend/static`` (NOT ``/app/static``) once the backend lives
# at ``/app/backend/app/``. Pre-f660262 the backend was at ``/app/app/``
# and the math landed at ``/app/static`` — the refactor moved the
# Python package but not the static destination, so HF Spaces silently
# served the SPA-fallback JSON for the SPA root path. The container
# still ran (``/health`` returned 200) and HF Spaces marked it
# ``running``, masking the regression.
COPY --from=frontend-builder /frontend/dist /app/backend/static/

ENV JOB_STORAGE_DIR=/tmp/app-jobs
ENV PYTHONPATH=/app/backend
# TRUSTED_PROXIES=* is HF SPACES SPECIFIC. The HF edge proxy strips
# the incoming X-Forwarded-For and re-emits its own, so "trust any
# upstream" is safe in that context. Anyone reusing this Dockerfile
# OUTSIDE of HF Spaces (self-hosted, custom k8s, behind a reverse
# proxy that does NOT sanitise X-Forwarded-For) MUST override this
# env var to either "127.0.0.1" (safe baseline) or the explicit
# proxy IP. Leaving the wildcard in a non-HF deployment lets any
# unauthenticated caller spoof X-Forwarded-For to bypass per-IP
# rate limits — making /api/providers/models a free
# credential-spray oracle (see L10/F5).
ENV TRUSTED_PROXIES=*

# Create non-root user and ensure storage dir is writable
RUN useradd --create-home appuser && mkdir -p /tmp/app-jobs && chown appuser /tmp/app-jobs
USER appuser

EXPOSE 7860

# No HEALTHCHECK instruction — HF Spaces performs its own HTTP health check
# on port 7860.  Adding a Docker HEALTHCHECK causes HF Spaces to wait for
# Docker's health state ("starting" → "healthy") instead of its own probe,
# which blocks the "Building" → "Running" transition indefinitely.

# Single worker on purpose: JobStore is in-process state, multi-worker
# would shard it across processes (job created on worker N invisible
# from worker M, SSE clients connecting to the wrong worker would
# never see updates). When a distributed JobStore lands (Redis,
# Postgres), bump `--workers` and `--limit-concurrency` together.
#
# `--limit-concurrency` caps the queue of in-flight requests so a slow
# LLM call doesn't accumulate connections indefinitely; surplus
# requests return 503 quickly. `--timeout-keep-alive` matches the SSE
# keepalive interval used by stream_events.
#
# Proxy-header handling lives in the Python middleware stack
# (backend/app/main.py installs ProxyHeadersMiddleware with the
# configurable TRUSTED_PROXIES env var). Previously we ALSO passed
# `--proxy-headers --forwarded-allow-ips=*` to uvicorn so the same
# rewrite happened twice; the second pass was a no-op but it
# silently widened trust to "any upstream", overriding whatever
# TRUSTED_PROXIES might be set to. Single layer is enough and keeps
# TRUSTED_PROXIES as the sole authority on which proxies to trust.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "7860", \
     "--workers", "1", \
     "--limit-concurrency", "100", \
     "--timeout-keep-alive", "60"]
