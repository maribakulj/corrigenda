# Stand-alone backend image used by docker-compose.
#
# Build context: repository root (see docker-compose.yml). Layout:
#   /app/packages/alto-core (editable install of the sibling package)
#   /app/backend             (this image's home)
# The requirements file declares `-e ../packages/alto-core`, so we cd
# into backend/ before `pip install`.
FROM python:3.11-slim
WORKDIR /app

COPY packages/alto-core /app/packages/alto-core
COPY backend/requirements.txt /app/backend/requirements.txt
WORKDIR /app/backend
RUN pip install --no-cache-dir -r requirements.txt
WORKDIR /app

COPY backend/app/ /app/backend/app/

ENV PYTHONPATH=/app/backend
RUN useradd --create-home appuser && mkdir -p /tmp/app-jobs && chown appuser /tmp/app-jobs
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
