"""Tests for the JSON logging formatter (Stage 4.C)."""

from __future__ import annotations

import json
import logging
import os
from io import StringIO

from app.observability.logging_config import JsonFormatter, setup_json_logging


def _capture(level: str = "INFO") -> tuple[logging.Logger, StringIO]:
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(f"test.json.{level}")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    return logger, buf


def test_basic_record_is_valid_json():
    logger, buf = _capture()
    logger.info("hello %s", "world")
    line = buf.getvalue().strip()
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.json.INFO"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload


def test_timestamp_is_iso8601_utc():
    logger, buf = _capture()
    logger.info("ts check")
    payload = json.loads(buf.getvalue().strip())
    # Format: 2025-01-01T12:34:56.789Z
    assert payload["timestamp"].endswith("Z")
    assert "T" in payload["timestamp"]


def test_exception_is_captured():
    logger, buf = _capture()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("caught")
    payload = json.loads(buf.getvalue().strip())
    assert "exception" in payload
    assert "RuntimeError" in payload["exception"]
    assert "boom" in payload["exception"]


def test_extra_fields_are_propagated():
    logger, buf = _capture()
    logger.info("with extra", extra={"job_id": "j-123", "chunks": 4})
    payload = json.loads(buf.getvalue().strip())
    assert payload["job_id"] == "j-123"
    assert payload["chunks"] == 4


def test_non_serialisable_extra_is_stringified():
    """A `extra={"obj": SomeClass()}` shouldn't crash the formatter."""
    logger, buf = _capture()

    class _Weird:
        def __repr__(self) -> str:
            return "<Weird()>"

    logger.info("weird", extra={"obj": _Weird()})
    payload = json.loads(buf.getvalue().strip())
    assert payload["obj"] == "<Weird()>"


def test_plain_format_via_env(monkeypatch):
    """LOG_FORMAT=plain should bypass JsonFormatter."""
    monkeypatch.setenv("LOG_FORMAT", "plain")
    setup_json_logging()
    root = logging.getLogger()
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)


def test_setup_is_idempotent(monkeypatch):
    """Calling setup twice replaces the handler instead of duplicating it."""
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    setup_json_logging()
    setup_json_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_uvicorn_access_log_silenced(monkeypatch):
    """Stage 4.C contract: noisy uvicorn.access logger drops to WARNING."""
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    setup_json_logging()
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def teardown_function(function):
    """Reset root logger between tests so we don't pollute other suites."""
    root = logging.getLogger()
    root.handlers[:] = []
    root.setLevel(logging.WARNING)
    os.environ.pop("LOG_FORMAT", None)
