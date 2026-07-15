"""Plan V2.4 — short-lived, purpose-scoped URL credentials.

The capability token used to travel as ``?token=`` for the surfaces
that cannot set headers (EventSource, ``<img>``). Query strings leak
into reverse-proxy/ingress/APM access logs — precisely the layer the
app is documented to sit behind and cannot redact. URL-borne auth is
now a SIGNED credential instead: HMAC-SHA256 over
``job_id:purpose:expiry`` with a per-process secret.

Properties:
- scoped to ONE job and ONE purpose ("events", "images") — a leaked
  credential can never download outputs or read the diff/trace;
- expiring — events credentials outlive one run, image credentials a
  few minutes;
- stateless — nothing stored, verification is a recomputation;
- per-process secret — a restart invalidates outstanding URLs, which
  is acceptable for an in-memory job store whose jobs die with the
  process anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

#: Regenerated at import time (per process). See module docstring for
#: why per-process is acceptable here.
_SECRET = secrets.token_bytes(32)


def _mac(job_id: str, purpose: str, exp: int) -> str:
    payload = f"{job_id}:{purpose}:{exp}".encode()
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()


def sign_url_credential(job_id: str, purpose: str, ttl_seconds: int) -> str:
    """Mint a ``<exp>.<mac>`` credential for ``?sig=``."""
    exp = int(time.time()) + ttl_seconds
    return f"{exp}.{_mac(job_id, purpose, exp)}"


def verify_url_credential(job_id: str, purpose: str, credential: str | None) -> bool:
    """Constant-time verification + expiry check. Never raises."""
    if not credential:
        return False
    exp_str, _, mac = credential.partition(".")
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    return hmac.compare_digest(mac, _mac(job_id, purpose, exp))
