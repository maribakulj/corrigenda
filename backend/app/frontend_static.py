"""Where the built SPA lives, and whether this deployment promises it.

The single-container image (root ``Dockerfile``) copies the built
frontend into ``backend/static/`` and sets ``SERVE_FRONTEND=1``; the
backend-only image (``backend/Dockerfile``, docker-compose dev) serves
no static files and leaves the variable unset.

The distinction matters for probes: a deployment that PROMISES a
frontend but lacks ``index.html`` is broken and must say so (503 on
``/`` and ``/health/ready``) — the historical regression this guards
against left ``/health`` returning 200 while the SPA root silently
served fallback JSON, and HF Spaces marked the Space "running".
"""

from __future__ import annotations

import os
from pathlib import Path

# Resolved once at import time — same process for the container's lifetime.
STATIC_DIR = Path(__file__).parent.parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


def frontend_expected() -> bool:
    """True iff this deployment promises to serve the built SPA.

    Read per call (not cached) so tests can monkeypatch the variable.
    """
    return os.environ.get("SERVE_FRONTEND", "").strip().lower() in {"1", "true", "yes"}
