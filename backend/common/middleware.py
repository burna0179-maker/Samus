"""HTTP middleware per doc §3.1.

- ``CorrelationMiddleware`` — propagates/generates ``X-Samus-Trace-Id``.
- ``MetricsMiddleware`` — records the doc's two HTTP instruments.
- ``VerifyHMACMiddleware`` — enforces HMAC-SHA256 on every inbound request
  except ``/health`` and ``/metrics``. Validates timestamp freshness, nonce
  uniqueness (in-process NonceStore), and signature via constant-time compare.
  After a successful verification it resolves the caller identity (from the
  signed ``X-Samus-Caller`` header) onto ``request.state.caller_service`` and
  applies the R-1 caller→callee authorization gate.
- ``NonceStore`` — 10 000-item LRU ring buffer for replay protection.

R-1 caller identity + authorization (2026-05-20)
------------------------------------------------
The verifier reads ``X-Samus-Caller``, resolves THAT caller's HMAC key
(per-service ``SAMUS_HMAC_KEY_<SERVICE>`` with shared-key fallback), and
verifies the signature with the caller name folded into the canonical string
— so a forged caller header invalidates the signature.

After a good signature, the resolved caller is attached to
``request.state.caller_service``. Exempt/unsigned paths (``/health``,
``/metrics``, the workcell's carved-out webhook paths) get the sentinel
``"external"``.

The coarse caller→callee authorization gate then runs under
``SAMUS_AUTHZ_MODE`` (off|audit|enforce — see ``capabilities.authz_mode``).
In the default ``off`` mode it is a no-op; in ``audit`` it logs would-be
denials; in ``enforce`` it returns 403. Per-service keys + the caller header
are ALWAYS active (additive, harmless); only the authz DECISION is gated by
the mode.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from typing import Any

_LOG = logging.getLogger("samus.middleware")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import capabilities, correlation, metrics, security
from .config import get_settings


class CorrelationMiddleware(BaseHTTPMiddleware):
    HEADER = "X-Samus-Trace-Id"

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        tid = request.headers.get(self.HEADER) or correlation.new_trace_id()
        correlation.set_trace_id(tid)
        response = await call_next(request)
        response.headers[self.HEADER] = tid
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, service_name: str | None = None) -> None:
        super().__init__(app)
        self._service = service_name or get_settings().service_name or "samus"

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        path = request.url.path
        method = request.method
        metrics.SAMUS_HTTP_REQUESTS_TOTAL.labels(
            self._service,
            method,
            path,
            str(response.status_code),
        ).inc()
        metrics.SAMUS_HTTP_REQUEST_DURATION_SECONDS.labels(
            self._service,
            method,
            path,
        ).observe(duration)
        return response


_IDEMPOTENCY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_IDEMPOTENCY_EXEMPT = frozenset({"/health", "/metrics", "/stripe_webhook", "/api/ses/feedback"})
_IDEMPOTENCY_HEADER = "Idempotency-Key"

# Paths where a missed dedup could double-charge a customer: FAIL CLOSED (503)
# when the idempotency store is unavailable rather than risk a duplicate. All
# other routes fail open to preserve availability. (Red/blue drill #6b, 2026-06-30.)
_IDEMPOTENCY_FAIL_CLOSED_PATHS = frozenset(
    p.strip()
    for p in os.getenv("SAMUS_IDEMPOTENCY_FAIL_CLOSED_PATHS", "/meter-event").split(",")
    if p.strip()
)
_IDEMPOTENCY_IN_FLIGHT_TTL = 30  # seconds to wait before treating a claim as orphaned


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """HTTP idempotency via ``Idempotency-Key`` header on mutation requests.

    On POST/PUT/PATCH with an ``Idempotency-Key`` header:

    - **First request:** claims the key in DynamoDB (atomic conditional write),
      processes normally, stores the response (status + body), returns it.
    - **Duplicate request (completed):** replays the stored response immediately
      with an ``X-Idempotency-Replayed: true`` header. No handler runs.
    - **Duplicate request (in-flight):** the key is claimed but no response is
      stored yet (another instance is processing). Returns ``409 Conflict`` with
      a ``Retry-After`` header so the client can poll.
    - **Missing header / exempt paths / non-mutation methods:** pass-through with
      no idempotency checks.

    Storage is DynamoDB (``ddb_idempotency_table``) with a 24-hour TTL so keys
    survive Cloud Run container restarts and work across multiple instances.
    An in-process LRU provides a fast-path skip of DynamoDB for keys seen in
    this process within the last 24 h.
    """

    def __init__(
        self,
        app: Any,
        *,
        workcell: str,
        exempt_paths: frozenset[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._workcell = workcell
        self._exempt = _IDEMPOTENCY_EXEMPT | (exempt_paths or frozenset())
        # Fast-path in-process cache: key -> "claimed" | dict(response)
        self._local: dict[str, Any] = {}
        self._local_lock = threading.Lock()

    def _local_get(self, pk: str) -> Any | None:
        with self._local_lock:
            return self._local.get(pk)

    def _local_set(self, pk: str, value: Any) -> None:
        with self._local_lock:
            self._local[pk] = value

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method not in _IDEMPOTENCY_METHODS:
            return await call_next(request)
        if request.url.path in self._exempt:
            return await call_next(request)

        idem_key = request.headers.get(_IDEMPOTENCY_HEADER, "").strip()
        if not idem_key:
            return await call_next(request)

        from . import dynamodb as _ddb

        pk = f"{self._workcell}:{idem_key}"

        # Fast-path: check local process cache first.
        local = self._local_get(pk)
        if local is not None and isinstance(local, dict):
            return Response(
                content=local["body"],
                status_code=local["status_code"],
                media_type=local["content_type"],
                headers={"X-Idempotency-Replayed": "true"},
            )

        # DynamoDB claim attempt.
        try:
            claimed = _ddb.idempotency_claim(self._workcell, idem_key)
        except Exception:
            # DynamoDB unavailable. Billing-affecting routes FAIL CLOSED — a
            # duplicate charge must never slip through during an outage. Every
            # other route fails open to preserve availability. (Drill #6b.)
            if request.url.path in _IDEMPOTENCY_FAIL_CLOSED_PATHS:
                _LOG.warning(
                    "idempotency store down on billing path %s — FAIL CLOSED (503)",
                    request.url.path,
                )
                return Response(
                    content=b'{"error":"idempotency_store_unavailable"}',
                    status_code=503,
                    media_type="application/json",
                    headers={"Retry-After": "5"},
                )
            _LOG.warning(
                "idempotency_claim failed for key=%r; proceeding without deduplication", idem_key
            )
            return await call_next(request)

        if not claimed:
            # Key already exists in DynamoDB — check if a response is ready.
            try:
                record = _ddb.idempotency_get(self._workcell, idem_key)
            except Exception:
                record = None

            if record and record.get("status") == "completed":
                cached: dict[str, Any] = {
                    "status_code": int(record["response_status"]),
                    "body": record["response_body"],
                    "content_type": record.get("content_type", "application/json"),
                }
                self._local_set(pk, cached)
                return Response(
                    content=cached["body"],
                    status_code=cached["status_code"],
                    media_type=cached["content_type"],
                    headers={"X-Idempotency-Replayed": "true"},
                )

            # Still in-flight (another instance claimed it but hasn't finished).
            return JSONResponse(
                {"error": "idempotency_conflict", "idempotency_key": idem_key},
                status_code=409,
                headers={"Retry-After": str(_IDEMPOTENCY_IN_FLIGHT_TTL)},
            )

        # We hold the claim — run the handler and capture the response.
        response = await call_next(request)

        # Read the response body (BaseHTTPMiddleware consumes it once).
        body_chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        body_bytes = b"".join(body_chunks)
        body_str = body_bytes.decode("utf-8", errors="replace")

        content_type = response.headers.get("content-type", "application/json")

        try:
            _ddb.idempotency_complete(
                self._workcell,
                idem_key,
                response.status_code,
                body_str,
                content_type,
            )
            self._local_set(
                pk,
                {
                    "status_code": response.status_code,
                    "body": body_str,
                    "content_type": content_type,
                },
            )
        except Exception:
            _LOG.warning(
                "idempotency_complete failed for key=%r; response returned but not cached",
                idem_key,
            )

        return Response(
            content=body_bytes,
            status_code=response.status_code,
            media_type=content_type,
            headers=dict(response.headers),
        )


class NonceStore:
    """LRU ring buffer for HMAC nonce dedupe. Single-process, not shared."""

    def __init__(self, max_items: int = 10_000) -> None:
        self._max = max_items
        self._ring: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._lock = threading.Lock()

    def seen(self, nonce: str) -> bool:
        if not nonce:
            return False
        now = time.time()
        with self._lock:
            if nonce in self._ring:
                return True
            self._ring[nonce] = now
            while len(self._ring) > self._max:
                self._ring.popitem(last=False)
            return False


_GLOBAL_NONCES = NonceStore()

# Maximum seconds a request timestamp may be AHEAD of the verifier's clock
# (S2 asymmetric replay window). Tight enough that a forward-skewed replay
# extension is rejected, loose enough to absorb sub-second NTP jitter between
# workcell containers on the same host. Deliberately not env-tunable: the
# stale bound (``hmac_window_seconds``) already absorbs real clock skew; the
# future bound only needs to cover same-host jitter.
_FUTURE_SKEW_SECONDS = 5


class VerifyHMACMiddleware(BaseHTTPMiddleware):
    """Enforce HMAC-SHA256 on inbound requests except ``/health``, ``/metrics``.

    The gateway service is exempted by name (it is the signer, not a verifier).

    ``extra_exempt_paths`` lets a workcell carve out additional paths whose
    handlers already enforce their own signature scheme (Stripe-Signature,
    X-Vapi-Signature, AWS SNS SigningCert) or that are intentionally public
    (the intake onboarding form). The carve-out is per-app-instance, so
    other workcells don't inherit a hole. Wired by ``create_base_app(
    hmac_exempt_paths=(...))``.
    """

    EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})

    def __init__(
        self,
        app: Any,
        *,
        secret: str | None = None,
        window_seconds: int | None = None,
        nonces: NonceStore | None = None,
        exempt_services: tuple[str, ...] = ("gateway",),
        extra_exempt_paths: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        settings = get_settings()
        # ``_shared_secret`` is the fallback key used when a caller has no
        # dedicated SAMUS_HMAC_KEY_<SERVICE>. An explicit ``secret=`` arg (used
        # by tests) pins the verifier to one key for ALL callers — in that
        # mode per-service resolution is bypassed.
        self._pinned_secret = secret
        self._shared_secret = secret or settings.shared_hmac_key
        self._window = window_seconds or settings.hmac_window_seconds
        self._nonces = nonces or _GLOBAL_NONCES
        self._exempt_services = exempt_services
        self._self_service = settings.service_name
        self._extra_exempt: frozenset[str] = frozenset(extra_exempt_paths or ())

    def _resolve_caller_key(self, caller: str) -> str:
        """Resolve the HMAC key the verifier expects ``caller`` to have signed with.

        When the middleware was constructed with an explicit ``secret=`` (the
        test path) that pinned key verifies every caller. Otherwise the
        caller's dedicated ``SAMUS_HMAC_KEY_<SERVICE>`` is used, falling back
        to the shared key — mirroring the signer's
        ``security.resolve_service_key``.
        """
        if self._pinned_secret is not None:
            return self._pinned_secret
        return security.resolve_service_key(caller, self._shared_secret)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # Exempt / unsigned paths: tag the caller as ``external`` (their
        # handler enforces its own auth scheme, or they are intentionally
        # public) and skip both verification and the authz gate.
        if request.url.path in self.EXEMPT_PATHS:
            request.state.caller_service = capabilities.EXTERNAL_CALLER
            return await call_next(request)
        if request.url.path in self._extra_exempt:
            request.state.caller_service = capabilities.EXTERNAL_CALLER
            return await call_next(request)
        if self._self_service in self._exempt_services:
            # The gateway (signer side) does not verify inbound HMAC. Its
            # inbound surface is the operator console, gated separately.
            request.state.caller_service = capabilities.EXTERNAL_CALLER
            return await call_next(request)

        ts = request.headers.get("X-Samus-Timestamp", "")
        nonce = request.headers.get("X-Samus-Nonce", "")
        sig = request.headers.get("X-Samus-Signature", "")
        if not ts or not nonce or not sig:
            return JSONResponse({"error": "hmac_headers_missing"}, status_code=401)

        # Caller identity (R-1). Absent header -> a pre-R-1 signer, which
        # signed with the SHARED key and the legacy (no-caller) canonical
        # string. With a header present, the caller signed with ITS key
        # (per-service, shared fallback) and folded its name into the MAC.
        caller_header = request.headers.get("X-Samus-Caller", "")
        caller = caller_header.strip() if caller_header else ""

        if caller:
            # R-1 signer: resolve the named caller's key.
            secret = self._resolve_caller_key(caller)
        else:
            # Pre-R-1 signer: shared key only. Never resolve a per-service
            # key here — a legacy signer has no caller identity and could not
            # have signed with anything but the shared secret.
            secret = self._pinned_secret if self._pinned_secret is not None else self._shared_secret
        if not secret:
            return JSONResponse({"error": "hmac_not_configured"}, status_code=503)

        try:
            ts_int = int(ts)
        except ValueError:
            return JSONResponse({"error": "hmac_timestamp_invalid"}, status_code=401)
        # Replay-window ASYMMETRY (S2). A symmetric ``abs(now - ts) > window``
        # check accepts a FUTURE-dated timestamp up to a full window ahead —
        # which lets a captured request stay replayable for ``window`` seconds
        # *past* its real send time by signing it with a clock skewed forward.
        # Split the bounds: a request may be at most ``window`` seconds STALE
        # (covers legitimate clock skew + transit) but no more than a small
        # ``_FUTURE_SKEW_SECONDS`` ahead of the verifier's clock. A correctly
        # clocked signer never trips the future bound; only a forward-skewed /
        # crafted timestamp does, and that is exactly the replay-extension we
        # are closing.
        now = int(time.time())
        if now - ts_int > self._window:
            return JSONResponse({"error": "hmac_timestamp_stale"}, status_code=401)
        if ts_int - now > _FUTURE_SKEW_SECONDS:
            return JSONResponse({"error": "hmac_timestamp_future"}, status_code=401)

        if self._nonces.seen(nonce):
            return JSONResponse({"error": "hmac_nonce_replay"}, status_code=401)

        body = await request.body()
        # When the request carries a caller header, fold it into the canonical
        # string (the signer did the same). When it does not — a pre-R-1
        # signer — pass ``caller=None`` so the legacy canonical string is
        # reconstructed and old signers keep verifying.
        sign_caller = caller if caller else None
        expected = security.sign_request(
            secret,
            request.method,
            request.url.path,
            ts,
            nonce,
            body,
            caller=sign_caller,
            query_string=request.url.query or "",
        )
        if not security.safe_compare(expected, sig):
            # Real-time operator push on a bad/forged signature. Rate-limited
            # HARD per caller (dedup TTL, ~5 min) so a burst of forged requests
            # pages once, not per request. Fail-soft; lazy import avoids cycles.
            try:
                from .notify import notify_operator

                _who = caller or "unknown"
                notify_operator(
                    "Auth signature mismatch",
                    f"HMAC signature verification failed for caller={_who} "
                    f"on {request.method} {request.url.path}",
                    severity="warning",
                    dedup_key=f"authfail:{_who}",
                )
            except Exception:  # noqa: BLE001 - alerting must never break the request
                pass
            return JSONResponse({"error": "hmac_signature_mismatch"}, status_code=401)

        # Signature is valid -> the caller identity is now PROVEN (the caller
        # name was inside the MAC). Attach it for downstream authorization.
        resolved_caller = caller or capabilities.UNKNOWN_CALLER
        request.state.caller_service = resolved_caller

        # R-1 coarse caller->callee authorization gate. No-op in the default
        # ``off`` mode; logs in ``audit``; 403s in ``enforce``. The callee is
        # this workcell. This is the boundary that subsumes the cross-workcell
        # IDOR (finding M2).
        allowed = capabilities.authorize_caller_to_callee(
            resolved_caller,
            self._self_service,
            path=request.url.path,
        )
        if not allowed:
            return JSONResponse(
                {
                    "error": "authz_denied",
                    "caller": resolved_caller,
                    "callee": self._self_service,
                },
                status_code=403,
            )

        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _receive  # type: ignore[attr-defined]
        return await call_next(request)
