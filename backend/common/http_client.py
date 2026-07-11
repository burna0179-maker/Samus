"""HMAC-signed inter-service HTTP client per doc §3.13.

Two entry points — one async, one sync.

- :func:`signed_post_json` is the canonical async path used by FastAPI
  routes that ``await`` cross-workcell calls inside their own request
  handler. Wraps the outbound POST in ``retry.retry_request`` for
  circuit-aware backoff.

- :func:`signed_post_json_sync` is the sync equivalent for producers
  that run in sync FastAPI handlers (or sync worker contexts) and need
  to fire-and-finish a dispatch before returning to their caller. Used
  by seo/proposal/finance to enqueue CRM dispatches to the gateway
  synchronously — the producer's request stays alive for the ~tens of
  ms the gateway needs to enqueue to SQS. This survives Cloud Run
  scale-to-zero (a daemon-thread alternative does not, because daemon
  threads spawned outside a tracked request can be killed when the
  instance scales down).

Both functions sign every request with shared HMAC secret + nonce +
timestamp and propagate the current trace id.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import correlation, retry, security
from .config import get_settings
from .net_limits import INTER_WORKCELL_MAX_BYTES, check_httpx_size


_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


def _build_signed_request(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    secret: str | None,
) -> tuple[str, bytes, dict[str, str]]:
    """Common request prep — returns (url, body_bytes, headers).

    Factored out of both ``signed_post_json`` and ``signed_post_json_sync``
    to keep signing logic in one place.

    R-1 (2026-05-20): the request now carries an ``X-Samus-Caller`` header
    naming the *calling* workcell, and the workcell signs with ITS OWN key
    (``SAMUS_HMAC_KEY_<SERVICE>``) when one is configured, falling back to
    the shared key when it is not. The caller name is folded into the HMAC
    canonical string so it cannot be forged. An explicit ``secret=`` argument
    still wins (the fulfillment DAG executor passes one), and when it does we
    skip per-service resolution — the caller has taken responsibility for the
    key. When ``secret`` is None the key is resolved from the caller's
    identity. All of this is additive: a stack with only the shared key
    behaves byte-identically to the pre-R-1 signer except for the new
    (signed-in) ``X-Samus-Caller`` header, which a pre-R-1 verifier simply
    ignores and an R-1 verifier folds back into the same canonical string.
    """
    settings = get_settings()
    caller = (getattr(settings, "service_name", "") or "unknown").strip() or "unknown"
    shared_key = getattr(settings, "shared_hmac_key", "") or ""
    if secret is None:
        secret = security.resolve_service_key(caller, shared_key)
    if not secret:
        raise RuntimeError(
            "signed_post_json requires an HMAC key — set "
            f"SAMUS_HMAC_KEY_{caller.upper()} or SAMUS_SHARED_HMAC_KEY"
        )
    url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    request = httpx.Request("POST", url, json=payload)
    body_bytes = request.content
    ts = security.generate_timestamp()
    nonce = security.generate_nonce()
    sig = security.sign_request(
        secret,
        "POST",
        path,
        ts,
        nonce,
        body_bytes,
        caller=caller,
    )
    trace_id = correlation.get_trace_id() or correlation.new_trace_id()
    headers = {
        "X-Samus-Timestamp": ts,
        "X-Samus-Nonce": nonce,
        "X-Samus-Signature": sig,
        "X-Samus-Caller": caller,
        "X-Samus-Trace-Id": trace_id,
        "Content-Type": "application/json",
    }
    return url, body_bytes, headers


async def signed_post_json(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    retries: int = 3,
    secret: str | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> httpx.Response:
    """Async POST with HMAC signature, retry, and circuit breaker."""
    url, body_bytes, headers = _build_signed_request(
        base_url,
        path,
        payload,
        secret=secret,
    )

    async def _call() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout or _DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, content=body_bytes, headers=headers)
            # S3: bound the peer response body before the caller decodes it.
            # A compromised peer workcell could otherwise return an unbounded
            # body and OOM this process. httpx has already buffered the body
            # here; the post-hoc guard raises on an over-cap body without
            # touching the response the caller goes on to use.
            check_httpx_size(
                resp,
                max_bytes=INTER_WORKCELL_MAX_BYTES,
                source="inter_workcell",
            )
            return resp

    return await retry.retry_request(_call, target=url, max_retries=retries, idempotent=True)


def signed_post_json_sync(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    retries: int = 2,
    secret: str | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> httpx.Response:
    """Sync POST with HMAC signature and bounded retries. No circuit breaker.

    Designed for sync producers (seo audit, proposal generation, finance
    Stripe webhook) that dispatch work to the gateway. The producer's
    request stays alive for the duration of this call (~tens of ms when
    the gateway is healthy), so the dispatch is durable across Cloud Run
    scale-to-zero — there's no detached background thread that can be
    killed mid-call.

    Retries are inline + bounded (default 2 attempts after the first =
    3 total) with simple exponential backoff (100ms, 400ms). The async
    circuit-breaker path is not reused because it requires an event loop;
    keeping this function pure-sync avoids ``asyncio.run`` ceremony at
    every call site.
    """
    import time

    url, body_bytes, headers = _build_signed_request(
        base_url,
        path,
        payload,
        secret=secret,
    )
    last_exc: Exception | None = None
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=timeout or _DEFAULT_TIMEOUT) as client:
                resp = client.post(url, content=body_bytes, headers=headers)
                # S3: bound the peer response body (see signed_post_json).
                check_httpx_size(
                    resp,
                    max_bytes=INTER_WORKCELL_MAX_BYTES,
                    source="inter_workcell",
                )
                return resp
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            # 100ms, 400ms backoff between attempts (matches the rough shape
            # of retry.retry_request but without its full circuit-breaker
            # state machine — sync producers are best-effort callers and
            # the gateway absorbs persistent failure as DLQ in any case).
            time.sleep(0.1 * (4**attempt))
    assert last_exc is not None  # mypy
    raise last_exc


def _build_signed_get_request(
    base_url: str,
    path: str,
    *,
    secret: str | None,
) -> tuple[str, dict[str, str]]:
    """GET variant of ``_build_signed_request`` — empty body, no Content-Type.

    Mirrors the POST signing scheme (security.sign_request gets ``method="GET"``
    and ``body_bytes=b""``) so the same VerifyHMACMiddleware accepts it. Added
    2026-06-22 to let prospecting fetch CRM call states without inventing a
    parallel auth path.
    """
    settings = get_settings()
    caller = (getattr(settings, "service_name", "") or "unknown").strip() or "unknown"
    shared_key = getattr(settings, "shared_hmac_key", "") or ""
    if secret is None:
        secret = security.resolve_service_key(caller, shared_key)
    if not secret:
        raise RuntimeError(
            "signed_get_json requires an HMAC key — set "
            f"SAMUS_HMAC_KEY_{caller.upper()} or SAMUS_SHARED_HMAC_KEY"
        )
    url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    ts = security.generate_timestamp()
    nonce = security.generate_nonce()
    sig = security.sign_request(secret, "GET", path, ts, nonce, b"", caller=caller)
    trace_id = correlation.get_trace_id() or correlation.new_trace_id()
    headers = {
        "X-Samus-Timestamp": ts,
        "X-Samus-Nonce": nonce,
        "X-Samus-Signature": sig,
        "X-Samus-Caller": caller,
        "X-Samus-Trace-Id": trace_id,
    }
    return url, headers


def signed_get_json_sync(
    base_url: str,
    path: str,
    *,
    retries: int = 1,
    secret: str | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> httpx.Response:
    """Sync GET with HMAC signature. No request body.

    Mirrors ``signed_post_json_sync``'s bounded-retry shape (default 1 retry
    = 2 total attempts; reads are idempotent so retries are safer than for
    POST, but the lookup is best-effort so we still keep the count small).
    """
    import time

    url, headers = _build_signed_get_request(base_url, path, secret=secret)
    last_exc: Exception | None = None
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=timeout or _DEFAULT_TIMEOUT) as client:
                resp = client.get(url, headers=headers)
                check_httpx_size(
                    resp,
                    max_bytes=INTER_WORKCELL_MAX_BYTES,
                    source="inter_workcell",
                )
                return resp
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            time.sleep(0.1 * (4**attempt))
    assert last_exc is not None  # mypy
    raise last_exc
