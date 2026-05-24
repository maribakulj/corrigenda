"""JSON-structured logging for the FastAPI backend.

Production deployments (HF Spaces, anywhere with a log collector)
need structured logs to extract fields like ``job_id``, ``level``, or
``exception`` without regex-parsing. We ship a stdlib-only formatter
so the package adds no dependency.

Usage (called once from :func:`app.main.create_app`):

    from app.observability.logging_config import setup_json_logging
    setup_json_logging()

The ``LOG_LEVEL`` env var controls verbosity (default INFO).
``LOG_FORMAT=plain`` falls back to the default human-readable format
— useful in local dev (e.g. when reading the terminal directly).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

# Standard LogRecord attributes — anything else on the record is treated
# as a custom field and propagated to the JSON output. Listing the
# stdlib set explicitly avoids both Python-version drift and accidental
# inclusion of internals.
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render every log record as a single-line JSON object.

    The shape is stable enough for downstream parsing:

        {"timestamp": "...", "level": "INFO", "logger": "...",
         "message": "...", "exception": "..." (optional),
         "<extra>": <any> (optional, from `logger.X(..., extra={...})`)}
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Propagate `extra={...}` fields the caller attached to the record.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # only include JSON-serialisable extras
            except TypeError:
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_json_logging(*, level: str | None = None) -> None:
    """Configure root logging once at app startup.

    Idempotent: re-calling replaces the existing handler instead of
    stacking duplicates. ``level`` overrides the ``LOG_LEVEL`` env
    var (which itself defaults to ``INFO``). Set ``LOG_FORMAT=plain``
    in the environment to keep stdlib's human-readable format
    (useful in dev — JSON is for shipped builds).
    """
    chosen_level: str = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    use_json = os.environ.get("LOG_FORMAT", "json").lower() != "plain"

    handler = logging.StreamHandler(sys.stdout)
    if use_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    root = logging.getLogger()
    # Replace any handlers we (or pytest, uvicorn pre-init, ...) added.
    root.handlers[:] = [handler]
    root.setLevel(chosen_level)
    # Quiet down third-party loggers that flood at INFO.
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("httpx").setLevel("WARNING")
