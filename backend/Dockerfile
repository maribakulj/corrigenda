# Stand-alone backend image used by docker-compose.
#
# Build context: repository root (see docker-compose.yml). Plan V4.3 —
# the full environment installs from the hash-locked file, then the
# corrigenda WHEEL installs with --no-deps (its dependencies can only
# come from the verified lock; the image ships the artefact users would
# get from PyPI, not an editable checkout).
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3
WORKDIR /app

COPY backend/requirements-lock.txt /app/backend/requirements-lock.txt
RUN pip install --no-cache-dir --require-hashes -r /app/backend/requirements-lock.txt

COPY packages/corrigenda /app/packages/corrigenda
RUN pip wheel --no-cache-dir --no-deps -w /tmp/wheels /app/packages/corrigenda \
    && pip install --no-cache-dir --no-deps /tmp/wheels/corrigenda-*.whl \
    && rm -rf /tmp/wheels

COPY backend/app/ /app/backend/app/

ENV PYTHONPATH=/app/backend
# TRUSTED_PROXIES intentionally NOT set here. The Python middleware
# defaults to "127.0.0.1" which is the safe choice for the docker-
# compose deployment (no edge proxy; the frontend talks directly to
# the backend over the Docker bridge network). Operators deploying
# this image behind a real reverse proxy must explicitly set the env
# var to the proxy's IP(s) — e.g. `TRUSTED_PROXIES=10.0.0.1` — or to
# "*" if the proxy itself strips/re-emits X-Forwarded-For. The
# previous unconditional `*` here was a holdover from the HF Space
# Dockerfile and exposed every self-hosted deployment to trivial
# rate-limit bypass via header spoofing (see L10/F5).
RUN useradd --create-home appuser && mkdir -p /tmp/app-jobs && chown appuser /tmp/app-jobs
USER appuser

EXPOSE 8000
# Single worker — JobStore is in-process state (see root Dockerfile).
# Concurrency cap protects against slow LLM calls accumulating
# connections; surplus requests return 503 quickly.
# Proxy-header handling: Python middleware only (see root Dockerfile
# comment for rationale).
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--limit-concurrency", "100", \
     "--timeout-keep-alive", "60"]
