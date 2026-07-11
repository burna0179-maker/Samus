"""Process-wide reusable ``httpx.Client`` instances for hot call sites.

The original Samus code constructed a fresh ``httpx.Client`` (and tore down
its connection pool) on every outbound HTTP call. Across the prospecting
crawler, enrichment Facebook fetch, the SEO audit, PageSpeed, security
probes, and the LLM client this allocates tens of thousands of sockets per
day — a TIME_WAIT and FD pressure source visible in long-running worker
RSS growth.

This module gives every hot call site a single keep-alive client to share.
The pattern is intentionally additive:

* Each hot module exposes its previous interface unchanged.
* Each acquires its client via :func:`get_shared_client` (or the dedicated
  factory below), and falls back to constructing a one-shot client when
  one is passed in for tests (the existing monkeypatch + factory-injection
  surfaces stay byte-identical for existing tests).

Clients live for the process lifetime. ``atexit`` closes them on shutdown
so leak detectors stay quiet. Each (timeout, http2, follow_redirects)
profile gets its own cached client because ``httpx`` does not allow per-
request override of these once the client is constructed.
"""

from __future__ import annotations

import atexit
import threading
from typing import Any

import httpx


_LOCK = threading.Lock()
_CLIENTS: dict[tuple[Any, ...], httpx.Client] = {}


def _timeout_key(timeout: Any) -> Any:
    """Reduce ``timeout`` to a hashable, identity-preserving cache key part.

    A scalar float/int is already hashable. An ``httpx.Timeout`` object is NOT
    hashable (2026-07-01 hang fix introduced phase-split ``httpx.Timeout`` at
    the crawler + SEO fetch sites), so flatten it to its four connect/read/
    write/pool components — two Timeouts with the same phases share one cached
    client, exactly as two equal scalars did.
    """
    if isinstance(timeout, httpx.Timeout):
        return (
            "httpx.Timeout",
            timeout.connect,
            timeout.read,
            timeout.write,
            timeout.pool,
        )
    return timeout


def _key(
    *,
    timeout: Any,
    http2: bool,
    follow_redirects: bool,
    headers_frozen: tuple[tuple[str, str], ...] | None,
    max_redirects: int | None,
) -> tuple[Any, ...]:
    return (
        _timeout_key(timeout),
        http2,
        follow_redirects,
        headers_frozen,
        max_redirects,
    )


def get_shared_client(
    *,
    timeout: float | httpx.Timeout = 10.0,
    http2: bool = False,
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
    max_redirects: int | None = None,
) -> httpx.Client:
    """Return a process-wide reusable ``httpx.Client`` matching the profile.

    A client is constructed lazily on first request for each unique
    (timeout, http2, follow_redirects, headers) tuple and reused thereafter.
    The connection pool inside the client is what we are actually trying to
    keep — repeated calls to the same host reuse keep-alive sockets instead
    of opening a new TCP+TLS handshake per request.

    Callers pass per-request ``headers`` to the client's ``get``/``post``
    method as usual when they vary; only headers that are part of the
    client's identity (e.g. a UA pinned at boot) belong here.
    """
    headers_frozen: tuple[tuple[str, str], ...] | None = (
        tuple(sorted(headers.items())) if headers else None
    )
    key = _key(
        timeout=timeout,
        http2=http2,
        follow_redirects=follow_redirects,
        headers_frozen=headers_frozen,
        max_redirects=max_redirects,
    )
    # Respect monkeypatching: re-read httpx.Client at call time, and if it
    # isn't the real httpx client class, return a fresh instance without
    # caching. Tests that patch httpx.Client with a fake (see e.g.
    # tests/test_prospecting_crawler.py::_seq_client) get a fresh fake per
    # call — matching the pre-hoist semantics where production constructed
    # a new client per request inside a ``with`` block.
    Client = httpx.Client
    is_real_client = getattr(Client, "__module__", "").startswith("httpx")
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "http2": http2,
        "follow_redirects": follow_redirects,
    }
    # ``max_redirects`` bounds a redirect chain so ``follow_redirects=True``
    # cannot multiply the wall clock on a hostile origin (2026-07-01 hang).
    # Only forwarded when the caller asks for it so the real httpx default
    # (and any stub-client signature) is otherwise untouched.
    if max_redirects is not None:
        kwargs["max_redirects"] = max_redirects
    if headers:
        kwargs["headers"] = headers
    if not is_real_client:
        return Client(**kwargs)
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is not None and not getattr(client, "is_closed", False):
            return client
        client = Client(**kwargs)
        _CLIENTS[key] = client
        return client


def close_all() -> None:
    """Close every cached client. Called from ``atexit`` and from tests."""
    with _LOCK:
        for client in _CLIENTS.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 — shutdown path, never raise
                pass
        _CLIENTS.clear()


atexit.register(close_all)


__all__ = ["get_shared_client", "close_all"]
