# Stand-alone backend image used by docker-compose.
#
# Build context: repository root (see docker-compose.yml). Two-step
# Python install: alto-core editable from the sibling path, then the
# backend's own requirements.txt (which no longer references alto-core
# — see Stage 6 of the audit remediation).
FROM python:3.11-slim
WORKDIR /app

COPY packages/alto-core /app/packages/alto-core
RUN pip install --no-cache-dir -e /app/packages/alto-core

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/app/ /app/backend/app/

ENV PYTHONPATH=/app/backend
# Trust X-Forwarded-For from any upstream when this image runs behind
# the docker-compose reverse proxy. See root Dockerfile + backend/app/main.py.
ENV TRUSTED_PROXIES=*
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
