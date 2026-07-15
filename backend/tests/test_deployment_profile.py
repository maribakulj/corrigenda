"""Plan V3.3 — explicit deployment profiles.

'demo' is the public-Space stance (wildcard CORS tolerated, documented
in SECURITY.md). 'institutional' asserts an SSO/reverse-proxy sits in
front and REFUSES demo-grade defaults instead of silently running with
them — a misconfigured institutional deployment must fail loudly at
startup, not leak quietly at runtime.
"""

from __future__ import annotations

import pytest

from app.main import create_app


def test_demo_profile_is_the_default_and_tolerates_wildcard_cors(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    create_app()  # must not raise


def test_institutional_profile_refuses_wildcard_cors(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "institutional")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        create_app()

    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        create_app()


def test_institutional_profile_starts_with_an_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "institutional")
    monkeypatch.setenv("CORS_ORIGINS", "https://corrigenda.example.org")
    create_app()  # must not raise


def test_unknown_profile_is_rejected(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production-ish")
    with pytest.raises(RuntimeError, match="DEPLOYMENT_PROFILE"):
        create_app()
