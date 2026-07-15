"""E2E harness — mock vendor apps, uvicorn-in-a-thread, client helpers.

The manual harness in ``tools/e2e/`` was the proof-run for these
scenarios (2026-07-13); this module powers the permanent pytest/CI
gate (Wave 0 of ``docs/audit/PLAN-CORRECTIONS.md``). The mock vendor
speaks the exact Mistral API dialect ``app/providers/mistral_provider.py``
expects, and the REAL backend app is launched under a real uvicorn
server with the Mistral base URL repointed at the mock — upload,
pipeline, SSE and download all cross a genuine HTTP boundary.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_XML = REPO_ROOT / "examples" / "sample.xml"

# ---------------------------------------------------------------------------
# Mock vendor apps (Mistral dialect)
# ---------------------------------------------------------------------------

# Deterministic "LLM": classic French OCR confusions found in sample.xml.
_FIXES = [
    (r"\bFrauce\b", "France"),
    (r"\buue\b", "une"),
    (r"\bcitoyeus\b", "citoyens"),
    (r"\bsoulevèreut\b", "soulevèrent"),
    (r"\bpouvolr\b", "pouvoir"),
    (r"\bjouruée\b", "journée"),
    (r"\bsou\b", "son"),
    (r"\bbouleYerse-", "bouleverse-"),
    (r"\buationale\b", "nationale"),
    (r"\bhoinme\b", "homme"),
    (r"\bcitoyeu\b", "citoyen"),
    (r"\bpriucipes\b", "principes"),
    (r"\btrausformer\b", "transformer"),
    (r"\btronblée\b", "troublée"),
    (r"\bmeuts\b", "ments"),
]


def _apply_fixes(text: str) -> str:
    for pat, rep in _FIXES:
        text = re.sub(pat, rep, text)
    return text


def _sabotage(line_id: str, text: str) -> str:
    """Deliberately invariant-violating corrections.

    If the pipeline's guards are real, none of these must reach the
    output XML; the lines must fall back to OCR and the job must end
    ``completed_with_fallbacks``.
    """
    if line_id == "TL4":
        # Fusion: PART1 swallows PART2's fragment — ends with the FULL
        # logical word 'dénonçait' instead of 'dénon-'.
        return "Le peuple réclamait la liberté et dénonçait"
    if line_id == "TL7":
        # Absorption: pulls the next physical line's words into this one.
        return "ments. L'assemblée nationale proclama"
    if line_id == "TL10":
        return ""  # emptied line
    return _apply_fixes(text)


def _build_vendor_app(
    model_id: str,
    correct: Callable[[str, str], str],
    completion_delay: float = 0.0,
) -> FastAPI:
    vendor = FastAPI()

    @vendor.get("/v1/models")
    async def models() -> dict:
        return {
            "data": [
                {
                    "id": model_id,
                    "name": model_id,
                    "capabilities": {"completion_chat": True},
                }
            ]
        }

    @vendor.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        if completion_delay:
            await asyncio.sleep(completion_delay)
        body = await request.json()
        user_payload = json.loads(body["messages"][1]["content"])
        lines_out = [
            {
                "line_id": ln["line_id"],
                "corrected_text": correct(ln["line_id"], ln.get("ocr_text", "")),
            }
            for ln in user_payload.get("lines", [])
        ]
        content = json.dumps({"lines": lines_out}, ensure_ascii=False)
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80},
        }

    return vendor


def build_honest_app() -> FastAPI:
    # The 0.5 s completion delay keeps the job alive long enough for the
    # SSE subscription (established right after POST /api/jobs returns)
    # to catch LIVE progress events; without it the mock answers in
    # microseconds and subscribers only ever see the synthetic terminal.
    return _build_vendor_app(
        "mock-mistral-small",
        lambda _lid, text: _apply_fixes(text),
        completion_delay=0.5,
    )


def build_sabotage_app() -> FastAPI:
    return _build_vendor_app("mock-sabotage", _sabotage)


def _absorption_only(line_id: str, text: str) -> str:
    """ONLY one absorption, on a NON-hyphenated line, no other corruption.

    In the combined saboteur, TL4's hyphen fusion (a chunk-level
    validation failure) makes the whole chunk fall back before TL7's
    payload ever reaches any acceptance guard — the review showed TL7's
    revert was collateral, not a guard proof. This vendor corrupts only
    TL2 (no hyphen role, so the hyphen reconciler cannot mask the
    verdict): the Stage-C absorption guard itself must revert it, as a
    PER-LINE fallback, while every other line's honest correction
    survives."""
    if line_id == "TL2":
        # TL2's (corrected) text + TL3's (corrected) text concatenated —
        # the exact source+next shape Guard 3 exists to catch.
        return (
            "La France traversa une période troublée. "
            "Les citoyens se soulevèrent contre l'oppression."
        )
    return _apply_fixes(text)


def build_absorption_only_app() -> FastAPI:
    return _build_vendor_app("mock-absorption", _absorption_only)


# ---------------------------------------------------------------------------
# uvicorn-in-a-thread helper (ephemeral port)
# ---------------------------------------------------------------------------


class UvicornThread:
    """Run an ASGI app under a real uvicorn server in a daemon thread."""

    def __init__(self, asgi_app: FastAPI) -> None:
        config = uvicorn.Config(asgi_app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)
        self.base_url = ""

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self._thread.is_alive():
                raise RuntimeError("uvicorn server thread died during startup")
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn server failed to start within 30s")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=15)


# ---------------------------------------------------------------------------
# Client-side helpers
# ---------------------------------------------------------------------------


def submit_job(base_url: str, model: str, xml_path: Path = SAMPLE_XML) -> dict:
    """POST /api/jobs with one ALTO file; return the JSON response."""
    with xml_path.open("rb") as fh:
        resp = httpx.post(
            f"{base_url}/api/jobs",
            files={"files": (xml_path.name, fh, "application/xml")},
            data={"provider": "mistral", "model": model, "api_key": "dummy"},
            timeout=60,
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def collect_sse_until_terminal(
    base_url: str, job_id: str, token: str, timeout: float = 120.0
) -> list[tuple[str, dict]]:
    """Consume the SSE stream until a terminal event; return all events.

    ``timeout`` is a WALL-CLOCK deadline, enforced explicitly: the
    server emits a keepalive every 30 s while a job is non-terminal, so
    a per-read httpx timeout alone would never fire and a job that
    fails to terminate (the exact regression class this gate exists to
    catch) would hang the test forever.
    """
    deadline = time.monotonic() + timeout
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    with httpx.stream(
        "GET",
        f"{base_url}/api/jobs/{job_id}/events",
        # Plan V2.4 — the capability token travels in a HEADER only (httpx
        # can set one, unlike a browser EventSource which uses ?sig=).
        headers={"X-Job-Token": token},
        # Per-read gap bound: keepalives arrive every 30 s, so 60 s of
        # silence means the stream itself is dead.
        timeout=60.0,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        for line in resp.iter_lines():
            if time.monotonic() > deadline:
                raise AssertionError(f"no terminal SSE event within {timeout}s: {events}")
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current_event is not None:
                raw = line.split(":", 1)[1].strip()
                data = json.loads(raw) if raw else {}
                events.append((current_event, data))
                if current_event in ("completed", "failed", "error"):
                    return events
                current_event = None
    raise AssertionError(f"SSE stream ended without a terminal event: {events}")


def download_xml(base_url: str, job_id: str, token: str) -> bytes:
    resp = httpx.get(
        f"{base_url}/api/jobs/{job_id}/download",
        headers={"X-Job-Token": token},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    return resp.content
