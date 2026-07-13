"""Launch the REAL backend with the Mistral base URL pointed at the local
mock vendor. Runtime patch only — the repository is not modified."""

import os
import sys

sys.path.insert(0, "/home/user/corrigenda/backend")
os.environ.setdefault("LOG_FORMAT", "plain")
os.environ.setdefault("LOG_LEVEL", "INFO")

import uvicorn

from app.providers import mistral_provider

# Point the provider at the local mock — runtime patch, repo untouched.
mistral_provider._BASE = "http://127.0.0.1:9611"

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8642, log_level="info")
