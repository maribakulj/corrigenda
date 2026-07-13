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


class RedactionFilter(logging.Filter):
    """P1-6 — central secret redaction, applied to EVERY record before any
    handler formats it.

    HTTP responses were already sanitised, but the runner logged the raw
    exception (``logger.exception`` BEFORE computing the safe message),
    the task registry logged raw tracebacks, and the formatters
    serialised both verbatim — so an API key embedded in a provider
    error message could reach the logs unmasked. Redacting in a root
    filter closes every path at once (message, formatted traceback,
    JSON extras), whatever the call site does.

    Uses the library's pattern-based ``sanitize_error`` (Bearer/Basic
    tokens, ``sk-``/``key-`` shapes, ``api_key=`` fragments, …); the
    exact-key replacement can't run here (the filter doesn't know the
    key), which is precisely why the patterns must be format-level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from corrigenda import sanitize_error

        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        redacted = sanitize_error(message)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = None
        if record.exc_info and record.exc_info != (None, None, None):
            # Format the traceback ONCE here, redact it, and stash it in
            # exc_text — logging.Formatter.format() reuses exc_text when
            # present instead of re-formatting exc_info.
            record.exc_text = sanitize_error(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        # Audit P3 — the JSON formatter copies every non-reserved record
        # attribute (logger.X(..., extra={...})) into the payload
        # verbatim, so a string extra like {"raw": "Authorization: Bearer
        # sk-…"} bypassed redaction entirely. Sanitise string-valued
        # extras here too, closing the docstring's promise.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if isinstance(value, str):
                setattr(record, key, sanitize_error(value))
        return True


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
        if record.exc_text:
            # P1-6 — the RedactionFilter pre-formats and redacts the
            # traceback into exc_text.
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Propagate `extra={...}` fields the caller attached to the record.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # only include JSON-serialisable extras
            except Exception:
                # Audit-F22 — a probe that raised only for TypeError let a
                # circular reference (ValueError) or a deeply-nested value
                # (RecursionError) escape and drop the whole record. A log
                # must NEVER kill its own record: fall back to repr() for
                # anything json.dumps refuses, and if repr() itself blows
                # up, degrade to a placeholder rather than propagate.
                try:
                    value = repr(value)
                except Exception:
                    value = "<unrepresentable>"
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
    # P1-6 — every record is redacted before ANY formatter sees it,
    # in the JSON shape and the plain dev shape alike.
    handler.addFilter(RedactionFilter())
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
