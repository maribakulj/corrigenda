"""Per-IP rate limiting (slowapi).

A single ``Limiter`` lives on ``app.state.limiter`` so endpoints share
the same backing storage. Two routes need protection today:

- ``POST /api/providers/models``: an unauthenticated caller can supply
  an arbitrary api_key and learn whether it's valid by inspecting the
  response. Without throttling, that's a credential-spray oracle.
- ``POST /api/jobs``: file uploads cost CPU and disk; abuse drowns
  the single-worker server.

The default in-memory storage is sufficient for the single-worker
HF Spaces deployment. A future move to multi-worker would need a
shared store (Redis) — slowapi supports that via ``storage_uri``.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# Global limiter. `key_func=get_remote_address` keys on the client IP
# (X-Forwarded-For respected when uvicorn runs behind a reverse proxy
# with `--proxy-headers`, which HF Spaces sets).
limiter = Limiter(key_func=get_remote_address)
