"""Tests for orchestrator._sanitize_error (T-005, B-010 regression)."""
from __future__ import annotations

from app.jobs.orchestrator import _sanitize_error


# ---------------------------------------------------------------------------
# Regex-based stripping
# ---------------------------------------------------------------------------

def test_strips_bearer_token():
    msg = "401 Unauthorized: Bearer sk-abcdefghijklmnop"
    out = _sanitize_error(msg)
    assert "sk-abcdefghijklmnop" not in out
    assert "Bearer ****" in out


def test_strips_openai_style_sk_token():
    msg = "Auth failed for sk-proj_AAAAABBBBBCCCCC1234567890"
    out = _sanitize_error(msg)
    # Pattern keeps the 'sk-' prefix + 4 chars, masks the rest
    assert "sk-proj****" in out
    assert "AAAAABBBBBCCCCC1234567890" not in out


def test_strips_mistral_style_key_token():
    msg = "Provider error: key-XYZW1234567890abcdef rejected"
    out = _sanitize_error(msg)
    assert "key-XYZW****" in out
    assert "XYZW1234567890abcdef" not in out


def test_handles_multiple_tokens_in_one_message():
    msg = "First Bearer sk-AAAAabcdefg then Bearer sk-BBBBabcdefg again"
    out = _sanitize_error(msg)
    assert "sk-AAAAabcdefg" not in out
    assert "sk-BBBBabcdefg" not in out
    assert out.count("Bearer ****") == 2


# ---------------------------------------------------------------------------
# api_key parameter
# ---------------------------------------------------------------------------

def test_masks_user_supplied_api_key():
    api_key = "user-secret-key-12345"
    msg = f"Request failed with {api_key} in payload"
    out = _sanitize_error(msg, api_key=api_key)
    assert api_key not in out
    # First 4 chars are kept as a hint
    assert "user****" in out


def test_short_api_key_not_masked():
    """An api_key under 9 chars is too short to be safely partial-masked
    (could mask non-key substrings). The function ignores it."""
    api_key = "shortkey"  # 8 chars — at the boundary
    msg = f"Bad request for shortkey provided"
    out = _sanitize_error(msg, api_key=api_key)
    # Behavior: the literal short api_key is left intact.
    assert "shortkey" in out


def test_no_api_key_passes_message_through():
    msg = "Generic error without any secret"
    assert _sanitize_error(msg) == msg
    assert _sanitize_error(msg, api_key=None) == msg


# ---------------------------------------------------------------------------
# B-010 regression — the caller in run_job must sanitize BEFORE truncating
# ---------------------------------------------------------------------------

def test_truncate_then_sanitize_loses_partial_key():
    """Documents the OLD broken ordering that B-010 fixed.

    If `str(exc)[:500]` chops the api_key in half, the substring lookup
    inside _sanitize_error misses it and half the secret leaks. The fix
    is to sanitize the FULL message then truncate. This test pins the
    correct order: sanitizing first redacts the key wherever it sits."""
    api_key = "user-secret-key-12345"
    # The key sits across what would be the truncation boundary.
    prefix = "X" * 490
    msg = f"{prefix}user-secret-key-12345 rest of message"

    # Wrong order (old behaviour): truncate first, then sanitize.
    truncated_first = _sanitize_error(msg[:500], api_key=api_key)
    # The truncated msg contains only "user-secre" (10 chars of the key),
    # which doesn't equal the full api_key → no substring replacement.
    # The regex doesn't match either (not sk-/key-/Bearer). Partial leak.
    assert "user-secre" in truncated_first

    # Right order (B-010 fix): sanitize first, then truncate.
    sanitized_first = _sanitize_error(msg, api_key=api_key)[:500]
    # Full key was replaced by "user****" before truncation — no leak,
    # even of the partial.
    assert "user-secret-key-12345" not in sanitized_first
    assert "user-secre" not in sanitized_first


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_message():
    assert _sanitize_error("") == ""


def test_handles_unicode():
    msg = "Erreur: Bearer sk-AAAAéàçabc rejeté"
    out = _sanitize_error(msg)
    assert "sk-AAAAéàçabc" not in out
    assert "Erreur" in out
