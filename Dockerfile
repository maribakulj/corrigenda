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

# Layout: /app/packages/alto-core (editable install) and /app/backend
# (the FastAPI app). The requirements file lives in backend/ and
# declares `-e ../packages/alto-core`, so the WORKDIR must be backend/
# during `pip install` for the relative path to resolve.
COPY packages/alto-core /app/packages/alto-core
COPY backend/requirements.txt /app/backend/requirements.txt
WORKDIR /app/backend
RUN pip install --no-cache-dir -r requirements.txt
WORKDIR /app

COPY backend/app/ /app/backend/app/
COPY --from=frontend-builder /frontend/dist /app/static/

ENV JOB_STORAGE_DIR=/tmp/app-jobs
ENV PYTHONPATH=/app/backend

# Create non-root user and ensure storage dir is writable
RUN useradd --create-home appuser && mkdir -p /tmp/app-jobs && chown appuser /tmp/app-jobs
USER appuser

EXPOSE 7860

# No HEALTHCHECK instruction — HF Spaces performs its own HTTP health check
# on port 7860.  Adding a Docker HEALTHCHECK causes HF Spaces to wait for
# Docker's health state ("starting" → "healthy") instead of its own probe,
# which blocks the "Building" → "Running" transition indefinitely.

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
