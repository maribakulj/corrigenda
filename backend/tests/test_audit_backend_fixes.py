"""Audit remediation (2026-07-12) — backend P1/P2/P3 fixes."""

from __future__ import annotations

import logging

import httpx
import pytest

from app.observability.logging_config import JsonFormatter, RedactionFilter
from app.providers.anthropic_provider import _compute_max_tokens, _model_output_cap
from app.providers.base import _wrap_if_transient


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example.com")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


# ---------------------------------------------------------------------------
# P3 — 408/425 are transient, not permanent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 425])
def test_self_healing_4xx_are_transient(status):
    from corrigenda.core.protocols import ProviderTransientError

    result = _wrap_if_transient(_http_status_error(status))
    assert isinstance(result, ProviderTransientError)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_stay_permanent(status):
    from corrigenda.core.protocols import ProviderPermanentError

    assert isinstance(_wrap_if_transient(_http_status_error(status)), ProviderPermanentError)


# ---------------------------------------------------------------------------
# P1 — Anthropic max_tokens never exceeds the model's real output cap
# ---------------------------------------------------------------------------


def test_model_output_caps():
    assert _model_output_cap("claude-3-haiku-20240307") == 4096
    assert _model_output_cap("claude-3-5-sonnet-20241022") == 8192
    assert _model_output_cap("claude-sonnet-4-20250514") == 64_000
    assert _model_output_cap("some-future-model") == 8192  # safe default


def test_max_tokens_clamped_below_model_cap():
    # A 60-line page chunk would compute 12000; claude-3.5's cap is 8192.
    payload = {"lines": [{"line_id": str(i)} for i in range(60)]}
    assert _compute_max_tokens(payload, "claude-3-5-sonnet-20241022") == 8192
    # claude-3-haiku cap is 4096 — even the floor must clamp down.
    assert _compute_max_tokens(payload, "claude-3-haiku-20240307") == 4096
    # A 4.x model with headroom scales up.
    assert _compute_max_tokens(payload, "claude-sonnet-4-20250514") == 12000


# ---------------------------------------------------------------------------
# P3 — string log extras are redacted
# ---------------------------------------------------------------------------


def test_log_extra_string_is_redacted():
    rec = logging.LogRecord(
        name="x",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="upstream rejected",
        args=None,
        exc_info=None,
    )
    rec.raw = "Authorization: Bearer sk-abcdef1234567890abcdef"  # extra
    RedactionFilter().filter(rec)
    import json

    payload = json.loads(JsonFormatter().format(rec))
    assert "sk-abcdef1234567890abcdef" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# P2 — ZIP declared-size precheck ignores non-extractable members
# ---------------------------------------------------------------------------


def test_declared_size_ignores_unextractable_members(tmp_path, monkeypatch):
    import io
    import zipfile

    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(storage_module, "_MAX_ZIP_EXTRACTED_BYTES", 50_000)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("page.xml", b"<alto/>" * 100)  # small, extractable
        zf.writestr("dataset.csv", b"x" * 200_000)  # large, skipped
    # Old code summed both -> 400. Now only page.xml counts -> accepted.
    saved, _ = storage_module.save_uploaded_files("j1", [("a.zip", buf.getvalue())])
    assert "page.xml" in saved
