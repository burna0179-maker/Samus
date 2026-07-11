"""BodySizeLimitMiddleware — S4 request-body ceiling for the inbound mesh.

Every Samus workcell route that reads the request body does so by fully
buffering it (``await request.body()`` in ``VerifyHMACMiddleware`` for the
signature check, then ``await request.json()`` in the handler). An unbounded
body is therefore a direct memory-exhaustion vector: an HMAC-valid caller — or
one compromised workcell — can POST a multi-gigabyte body and the verifier
buffers the whole thing before it can even reject a bad signature. On the
gateway (the revenue-bearing, operator-facing surface) that is a containment-
defeat DoS.

This raw-ASGI middleware caps the body two ways (mirrors Darwin's
``body_size_limit.py`` adapted to Samus's env-driven config idiom):

1. **Fast reject on Content-Length** — a declared length over the cap (or a
   malformed Content-Length) answers ``413`` before a single body byte is read.
2. **Streaming ceiling** — for chunked / Content-Length-absent bodies the
   inbound ``receive`` channel is wrapped so cumulative bytes are counted;
   crossing the cap raises before the handler (or the HMAC verifier) can buffer
   the whole body.

It is raw ASGI (not ``BaseHTTPMiddleware``) precisely so it can wrap
``receive`` without consuming the body — a ``BaseHTTPMiddleware`` would have to
read the body to inspect it, breaking the downstream HMAC body read.

The cap is read at request time from ``SAMUS_MAX_REQUEST_BODY_BYTES`` (default
1 MiB) so an operator can flip it without a redeploy. A cap of ``0`` is an
explicit opt-out (short-circuits untouched). Read-only methods
(GET/HEAD/OPTIONS) short-circuit too — they carry no body worth capping.

Fail-closed: any ambiguity (bad Content-Length, mid-stream overflow) ends in a
``413``, never a pass-through.

This middleware is added OUTERMOST in ``create_base_app`` so the cap runs
before ``VerifyHMACMiddleware`` buffers the body for signature verification.
``/health`` and ``/metrics`` are GETs and are skipped by method anyway.
"""
from __future__ import annotations

import json
import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Methods that never carry a body worth capping.
_SKIPPED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_ENV_MAX_BYTES = "SAMUS_MAX_REQUEST_BODY_BYTES"
_DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


def _configured_limit() -> int:
    """Resolve the body cap from the environment at request time.

    Honors ``SAMUS_MAX_REQUEST_BODY_BYTES``; falls back to 1 MiB. A
    non-integer value falls back to the default rather than failing open.
    """
    raw = os.getenv(_ENV_MAX_BYTES, "").strip()
    if not raw:
        return _DEFAULT_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MAX_BYTES


class _BodyTooLarge(Exception):
    """Internal signal raised by the wrapped receive on overflow."""

    def __init__(self, received: int, limit: int) -> None:
        super().__init__(f"request body exceeds {limit} bytes")
        self.received = received
        self.limit = limit


class BodySizeLimitMiddleware:
    """Reject request bodies larger than the configured byte ceiling."""

    def __init__(self, app: ASGIApp, *, max_bytes: int | None = None) -> None:
        self.app = app
        self._max_bytes = max_bytes

    def _limit(self) -> int:
        if self._max_bytes is not None:
            return int(self._max_bytes)
        return _configured_limit()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method", "GET").upper() in _SKIPPED_METHODS:
            await self.app(scope, receive, send)
            return

        limit = self._limit()
        if limit <= 0:  # operator opt-out
            await self.app(scope, receive, send)
            return

        # 1. Fast reject on a declared Content-Length.
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    await self._reject(scope, send, limit)
                    return
                if declared > limit:
                    await self._reject(scope, send, limit)
                    return
                break

        # 2. Streaming ceiling for chunked / CL-absent bodies.
        received = 0

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > limit:
                    raise _BodyTooLarge(received, limit)
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except _BodyTooLarge:
            # The handler / verifier tried to read past the cap. If the
            # response had not begun (the common case — bodies are read before
            # any output), answer 413; otherwise the stream is committed and
            # the error must propagate to the server.
            if response_started:
                raise
            await self._reject(scope, send, limit)

    async def _reject(self, scope: Scope, send: Send, limit: int) -> None:
        payload = json.dumps(
            {
                "error": "request_body_too_large",
                "detail": f"request body exceeds the {limit}-byte limit",
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


__all__ = ["BodySizeLimitMiddleware"]
