"""ASGI upload-size guard (Audit-F18).

The in-handler byte caps in ``create_job`` run only AFTER Starlette has
awaited ``request.form()`` — by which point the multipart parser has
already spooled every file part to disk (a ``SpooledTemporaryFile`` with
no total-size limit). An unauthenticated attacker could therefore write
terabytes to the job filesystem before any guard fired — a disk-
exhaustion DoS on the single-worker server whose ``/tmp/app-jobs`` shares
the same volume.

This pure-ASGI middleware runs BEFORE form parsing:

1. **Content-Length fast path.** A missing or over-cap ``Content-Length``
   on a guarded POST is rejected with a clean 413 before a byte of the
   body is read. (File uploads from browsers and ``curl`` always send a
   Content-Length; refusing its absence closes the streamed-body bypass.)
2. **Streaming byte counter.** For a body whose Content-Length UNDER-
   declares the payload, a running counter aborts the request once the
   received bytes cross the cap and the middleware ANSWERS THE 413
   ITSELF (Plan V2.3). The previous behaviour — handing the app a fake
   empty end-of-body — made the response depend on whatever the
   multipart parser produced (400/422/form error) instead of the size
   rejection this guard promises. ``Connection: close`` is set so the
   server drops the connection instead of draining the oversized
   remainder.

The in-handler caps stay as defence in depth.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable

_DEFAULT_MAX_REQUEST_BYTES = 256 * 1024 * 1024  # 256 MiB (200 MiB payload + overhead)
# Wave-3 review — JSON-body endpoints need a cap too: FastAPI's
# ``await request.body()`` accumulates the whole stream IN MEMORY, so a
# chunked request with no Content-Length OOMed the single worker through
# /api/providers/models even though /api/jobs was guarded. Their bodies
# are tiny (a provider name + key), so the cap is far tighter.
_DEFAULT_MAX_JSON_REQUEST_BYTES = 1024 * 1024  # 1 MiB


def _max_request_bytes() -> int:
    """Resolve the upload cap dynamically so it can be overridden
    per-deployment (``MAX_REQUEST_BYTES``) and monkeypatched in tests."""
    raw = os.environ.get("MAX_REQUEST_BYTES")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_MAX_REQUEST_BYTES


def _max_json_request_bytes() -> int:
    """Cap for guarded JSON-body routes (``MAX_JSON_REQUEST_BYTES``)."""
    raw = os.environ.get("MAX_JSON_REQUEST_BYTES")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_MAX_JSON_REQUEST_BYTES


#: Path prefixes whose POST bodies are size-guarded → cap resolver.
#: Lambdas (not bare references) so tests monkeypatching the module
#: functions are honoured — the name resolves at call time.
_GUARDED_POST_PATHS: dict[str, Callable[[], int]] = {
    "/api/jobs": lambda: _max_request_bytes(),
    "/api/providers/models": lambda: _max_json_request_bytes(),
}


def _guarded_cap(scope: dict) -> Callable[[], int] | None:
    """The cap resolver for a guarded scope, or ``None`` when unguarded."""
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return None
    path = (scope.get("path") or "").rstrip("/") or "/"
    for prefix, resolver in _GUARDED_POST_PATHS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return resolver
    return None


class UploadSizeLimitMiddleware:
    """Reject over-cap uploads before Starlette spools the body to disk."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        cap_resolver = _guarded_cap(scope)
        if cap_resolver is None:
            await self.app(scope, receive, send)
            return

        max_bytes = cap_resolver()
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        content_length = headers.get(b"content-length")

        if content_length is None:
            await _send_413(
                send,
                "Content-Length header is required for uploads.",
            )
            return
        try:
            declared = int(content_length)
        except ValueError:
            await _send_413(send, "Malformed Content-Length header.")
            return
        if declared > max_bytes:
            await _send_413(
                send,
                f"Upload exceeds the maximum request size ({max_bytes} bytes).",
            )
            return

        # Streaming guard: a lying Content-Length (declares small, sends
        # large) is caught by counting received bytes and aborting. The
        # middleware owns the response (413), not the multipart parser:
        # the exception raised from ``receive`` may be converted by the
        # framework (FastAPI wraps body-parse errors into a 400), so any
        # response the app produces AFTER the trip is dropped and
        # replaced with the promised 413.
        received = 0
        tripped = False
        response_started = False  # a response.start actually sent to the client

        async def counting_receive() -> dict:
            nonlocal received, tripped
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    tripped = True
                    raise _BodyCapExceeded()
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal response_started
            if tripped and not response_started:
                # Cap fired before any byte hit the wire: this
                # middleware owns the response — swallow whatever error
                # the framework cooked from the aborted body parse.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyCapExceeded:
            pass  # propagated raw through the app — handled below
        if tripped and not response_started:
            await _send_413(
                send,
                f"Upload exceeds the maximum request size ({max_bytes} bytes).",
                close_connection=True,
            )


class _BodyCapExceeded(Exception):
    """Raised by the counting receive when the body outgrows the cap.

    It may propagate raw back to the middleware, or be converted into a
    framework error response along the way — both paths end in the
    middleware's own 413 (see ``tracking_send``).
    """


async def _send_413(
    send: Callable[[dict], Awaitable[None]],
    detail: str,
    *,
    close_connection: bool = False,
) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if close_connection:
        # The client is mid-way through an oversized body: draining it
        # would defeat the guard, so tell the server to drop the
        # connection instead of reusing it.
        headers.append((b"connection", b"close"))
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
