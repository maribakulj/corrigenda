"""Plan V2.3 — the streaming size guard answers its own 413.

A Content-Length that UNDER-declares the payload used to make the
middleware hand the app a fake empty end-of-body: the client's response
then depended on whatever the multipart parser produced (400/422/form
error) instead of the size rejection the guard promises. The middleware
now owns the response: 413 + ``Connection: close`` (the oversized
remainder is dropped with the connection, never drained).

TestClient/httpx refuse to send a lying Content-Length, so these tests
drive the real ASGI stack directly.
"""

from __future__ import annotations

import asyncio

import pytest

from app.main import create_app


def _multipart_body(data_bytes: int) -> bytes:
    head = (
        b"--XX\r\n"
        b'Content-Disposition: form-data; name="files"; filename="a.xml"\r\n'
        b"Content-Type: application/xml\r\n\r\n"
    )
    return head + b"d" * data_bytes + b"\r\n--XX--\r\n"


async def _post_jobs(app, body: bytes, declared: int, chunk: int = 300) -> list[dict]:
    """POST /api/jobs through the raw ASGI interface; returns sent messages."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/jobs",
        "raw_path": b"/api/jobs",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "server": ("test", 80),
        "client": ("1.2.3.4", 1),
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"multipart/form-data; boundary=XX"),
            (b"content-length", str(declared).encode("ascii")),
        ],
        "app": app,
    }
    chunks = iter([body[i : i + chunk] for i in range(0, len(body), chunk)])

    async def receive() -> dict:
        try:
            return {"type": "http.request", "body": next(chunks), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _response_start(sent: list[dict]) -> dict:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, f"expected exactly one response.start, got {starts}"
    return starts[0]


def test_lying_content_length_gets_the_promised_413(monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BYTES", "1000")
    app = create_app()

    # Declares 500 bytes, actually streams ~2000 (> 1000 cap).
    sent = asyncio.run(_post_jobs(app, _multipart_body(1900), declared=500))

    start = _response_start(sent)
    assert start["status"] == 413, "size guard must answer 413, not a parser error"
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    # The oversized remainder must not be drained — drop the connection.
    assert headers.get("connection") == "close"
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"maximum request size" in body


def test_honest_body_within_cap_is_untouched(monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BYTES", "1000000")
    app = create_app()

    body = _multipart_body(200)
    sent = asyncio.run(_post_jobs(app, body, declared=len(body)))

    # The request reaches the handler (which 422s on the missing form
    # fields — anything but the middleware's 413 proves pass-through).
    assert _response_start(sent)["status"] != 413


@pytest.mark.parametrize("declared", [2000, 10_000])
def test_declared_over_cap_keeps_the_fast_path_413(monkeypatch, declared):
    monkeypatch.setenv("MAX_REQUEST_BYTES", "1000")
    app = create_app()

    sent = asyncio.run(_post_jobs(app, _multipart_body(100), declared=declared))

    start = _response_start(sent)
    assert start["status"] == 413
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    # Fast path: no body was consumed, the connection may be reused.
    assert headers.get("connection") is None
