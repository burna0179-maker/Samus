"""Bounded reads for OUTBOUND HTTP responses (S3, resource-exhaustion).

Samus's inbound surface is body-capped by ``BodySizeLimitMiddleware``, but
that does nothing for the responses Samus reads as a *client* from the
cross-agent quorum hub, peer workcells, the Anthropic API, the resource
broker, and arbitrary third-party sites (SEO/prospecting crawls). Every bare
``resp.read()`` / ``resp.json()`` / ``resp.text`` reads the entire response
into memory unbounded — so a compromised peer, a hostile hub, or a malicious
crawl target could return a multi-gigabyte body and OOM the workcell (a
denial-of-service that, on the gateway, takes the revenue path down).

Two entry points, one per transport Samus actually uses:

  * :func:`read_capped` — for stdlib ``urllib`` responses (the vendored
    ``quorum_client``). Reads at most ``max_bytes + 1`` so an over-cap body is
    detected without materialising it, then raises :class:`ResponseTooLarge`.

  * :func:`check_httpx_size` — for ``httpx.Response`` objects. A *post-hoc*
    guard: after a normal buffered httpx call, it inspects the already-read
    ``.content`` (and a declared ``Content-Length``) and raises
    :class:`ResponseTooLarge` when the body exceeds the cap — WITHOUT
    consuming or replacing the body. The caller keeps using ``resp.json()`` /
    ``resp.text`` / ``resp.content`` exactly as before. This mirrors Major's
    donor pattern (``core.net_limits.check_httpx_size``) and stays robust to
    minimal test doubles: a response-like object without ``.content`` is a
    no-op rather than a crash.

The caps are deliberately generous — the goal is a hard ceiling that bounds
peak memory, not a tight quota. Stdlib + httpx only; no new dependency.
"""
from __future__ import annotations

from typing import Any

import httpx

__all__ = [
    "ResponseTooLarge",
    "read_capped",
    "check_httpx_size",
    "QUORUM_HUB_MAX_BYTES",
    "INTER_WORKCELL_MAX_BYTES",
    "LLM_MAX_BYTES",
    "BROKER_MAX_BYTES",
    "CRAWL_MAX_BYTES",
]

# Per-channel ceilings. Generous headroom over real payloads.
QUORUM_HUB_MAX_BYTES: int = 4 * 1024 * 1024        # hub JSON-RPC is tiny
INTER_WORKCELL_MAX_BYTES: int = 16 * 1024 * 1024   # CRM/funnel admin proxies
LLM_MAX_BYTES: int = 16 * 1024 * 1024              # Anthropic message bodies
BROKER_MAX_BYTES: int = 1 * 1024 * 1024            # reserve/release envelopes
CRAWL_MAX_BYTES: int = 32 * 1024 * 1024            # third-party site fetches


class ResponseTooLarge(Exception):
    """Raised when an outbound HTTP response exceeds its byte ceiling."""


def read_capped(resp: Any, *, max_bytes: int, source: str = "response") -> bytes:
    """Read at most ``max_bytes`` from a ``urllib`` response; raise if larger.

    ``resp`` is any object with ``read(n)`` (a urllib ``HTTPResponse`` or an
    ``HTTPError``). Reads ``max_bytes + 1`` so an over-cap body is detected
    without materialising the whole thing.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")
    data = resp.read(max_bytes + 1)
    if data is not None and len(data) > max_bytes:
        raise ResponseTooLarge(
            f"{source} response exceeded {max_bytes} bytes (DoS guard)"
        )
    return data or b""


def check_httpx_size(
    resp: "httpx.Response", *, max_bytes: int, source: str = "response",
) -> None:
    """Post-hoc byte-cap for an ``httpx.Response``; raise if the body is larger.

    This is the donor (Major) pattern: the caller has already made a normal
    *buffered* httpx call and is about to use ``resp.json()`` / ``resp.text`` /
    ``resp.content`` as usual. This guard inspects the body that httpx already
    materialised and raises :class:`ResponseTooLarge` when it exceeds the cap —
    it does NOT consume or replace the body, so the caller's existing decode
    path is untouched.

    Two checks, both defensive against minimal test doubles:
      1. A declared ``Content-Length`` over the cap is rejected up front. Access
         is guarded with ``getattr`` + duck-typed ``.get`` so a response-like
         object without ``.headers`` is a no-op for this check.
      2. The already-read ``.content`` is measured against the cap. Access is
         guarded with ``getattr`` (and ``httpx.ResponseNotRead`` for the rare
         streamed case) so a minimal mock that omits ``.content`` is a no-op
         rather than a crash. The real ``httpx.Response`` always exposes
         ``.content`` after a buffered call, so the cap is fully enforced in
         production.

    Returns ``None``. Raises :class:`ResponseTooLarge` on overflow.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")

    # Content-Length pre-check (defensive; ``.content`` below is authoritative).
    headers = getattr(resp, "headers", None)
    declared = headers.get("content-length") if hasattr(headers, "get") else None
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ResponseTooLarge(
                    f"{source} declared Content-Length {declared} exceeds "
                    f"{max_bytes} bytes (DoS guard)"
                )
        except ValueError:
            # Malformed Content-Length — fall through to the body check, which
            # is the authoritative ceiling regardless of the header.
            pass

    # Authoritative check on the buffered body. ``getattr`` defaults to ``None``
    # for a partial mock that lacks ``.content``; ``httpx.ResponseNotRead`` is
    # raised by a genuine streamed (``stream=``) response whose body hasn't been
    # read — neither case can be measured here, so treat both as a no-op.
    try:
        content = getattr(resp, "content", None)
    except httpx.ResponseNotRead:
        content = None

    if content is not None and len(content) > max_bytes:
        raise ResponseTooLarge(
            f"{source} response exceeded {max_bytes} bytes (DoS guard)"
        )
