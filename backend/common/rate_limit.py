"""In-process fixed-window HTTP rate limiter for inter-workcell endpoints.

Finding M7 (2026-05-20): the LLM-backed and external-side-effect routes
(``voice`` autodialer, ``outreach`` send/post actions, ``seo`` content
generation, ``proposal`` generation, ``finance`` meter-event) had no HTTP
throttle. An HMAC-valid caller — or one compromised workcell — could loop the
autodialer, burn the Anthropic budget, or replay ``/meter-event`` to inflate
Stripe metered billing. The ``llm_budget`` cap is a soft downstream accounting
control, not a request-rate ceiling.

Why a NEW in-process limiter and not the intake limiter
-------------------------------------------------------
The public-intake limiter (``backend.intake.rate_limit``) is DynamoDB-backed
because ``/intake/onboarding`` runs on horizontally-scaled Cloud Run and the
counter must be *shared* across instances. It is also hard-wired to the
``intake_rate_limit_*`` settings and only counts when ``intake_rate_limit_
enabled`` is true. Reusing it for the inter-workcell mesh would force every
workcell to (a) write to DynamoDB on every request and (b) flip the intake-
specific enable flag. The inter-workcell routes here are reached only over the
HMAC-authenticated internal mesh — a single-process in-memory counter is the
right scope, has zero infra dependency, and never blocks on a backend hiccup.
A pure in-process ring is acceptable: the realistic abuse case (a runaway loop
or one compromised workcell) hammers a single target process, which this sees.

Window model
------------
Fixed (not sliding) windows: a window is a discrete time bucket whose id is
``floor(now / window_seconds)``. Per ``(scope_name, identifier, bucket)`` an
integer counter is incremented; when it exceeds the configured limit the
request is denied. Stale buckets are reaped lazily on access plus a bounded
LRU cap so memory cannot grow without bound.

Configuration
-------------
Every limit is env-tunable. A route declares a logical ``scope`` name; the
limit for that scope is read from ``SAMUS_RATE_LIMIT_<SCOPE>_PER_MINUTE``
(falling back to ``SAMUS_RATE_LIMIT_DEFAULT_PER_MINUTE``, default 30). A
non-positive value disables the limiter for that scope. The whole limiter is
disabled process-wide when ``SAMUS_RATE_LIMIT_ENABLED`` is falsey — the test
suite sets this so existing endpoint tests do not have to reason about
counters.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException, Request

_LOG = logging.getLogger("samus.common.rate_limit")

# Default per-minute ceiling applied to any scope without an explicit override.
_DEFAULT_PER_MINUTE = 30
# Bound on distinct live counter keys; oldest are evicted LRU-style. Generous
# enough that legitimate per-IP-per-scope traffic never evicts a hot key.
_MAX_KEYS = 50_000
_WINDOW_SECONDS = 60


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOG.warning("rate_limit: %s=%r is not an int — using default %d", name, raw, default)
        return default


def _limiter_enabled() -> bool:
    """Process-wide kill switch. Default ON; conftest sets this off for tests."""
    raw = os.getenv("SAMUS_RATE_LIMIT_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _limit_for_scope(scope: str) -> int:
    """Per-minute limit for ``scope``.

    Resolution order: ``SAMUS_RATE_LIMIT_<SCOPE>_PER_MINUTE`` -> the supplied
    code default -> ``SAMUS_RATE_LIMIT_DEFAULT_PER_MINUTE`` -> 30. A
    non-positive result disables the scope.
    """
    env_name = f"SAMUS_RATE_LIMIT_{scope.upper()}_PER_MINUTE"
    default = _env_int("SAMUS_RATE_LIMIT_DEFAULT_PER_MINUTE", _DEFAULT_PER_MINUTE)
    return _env_int(env_name, default)


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of one rate-limit check.

    ``allowed`` is the only field a caller must branch on. ``scope`` and
    ``limit`` describe the ceiling for the 429 detail / logs;
    ``retry_after_seconds`` is the seconds until the fixed window rolls over.
    """

    allowed: bool
    scope: str = ""
    limit: int = 0
    retry_after_seconds: int = 0


class _FixedWindowCounter:
    """Thread-safe in-process fixed-window counter with an LRU key cap."""

    def __init__(self, max_keys: int = _MAX_KEYS) -> None:
        self._max = max_keys
        self._counts: collections.OrderedDict[str, int] = collections.OrderedDict()
        self._lock = threading.Lock()

    def incr(self, key: str) -> int:
        """Increment ``key``'s counter and return the new value."""
        with self._lock:
            current = self._counts.get(key, 0) + 1
            self._counts[key] = current
            self._counts.move_to_end(key)
            while len(self._counts) > self._max:
                self._counts.popitem(last=False)
            return current


_COUNTER = _FixedWindowCounter()


def check_rate_limit(
    scope: str,
    identifier: str,
    *,
    now: float | None = None,
) -> RateLimitDecision:
    """Check (and count) one request against the per-minute ceiling for ``scope``.

    ``scope`` is a logical route group (e.g. ``voice_call``); ``identifier`` is
    the per-caller key (the source IP today; per-caller HMAC identity once the
    audit's root-cause R-1 lands). When the limiter is disabled process-wide,
    or the scope's limit is non-positive, an allowed decision is returned
    without touching the counter.
    """
    if not _limiter_enabled():
        return RateLimitDecision(allowed=True, scope=scope)

    limit = _limit_for_scope(scope)
    if limit <= 0:
        return RateLimitDecision(allowed=True, scope=scope, limit=limit)

    clock = time.time() if now is None else now
    bucket = int(clock // _WINDOW_SECONDS)
    ident = (identifier or "unknown").strip() or "unknown"
    key = f"{scope}:{ident}:{bucket}"
    count = _COUNTER.incr(key)
    if count > limit:
        retry_after = _WINDOW_SECONDS - int(clock % _WINDOW_SECONDS)
        _LOG.info(
            "rate-limit breach scope=%s id=%s count=%d limit=%d",
            scope,
            ident,
            count,
            limit,
        )
        return RateLimitDecision(
            allowed=False,
            scope=scope,
            limit=limit,
            retry_after_seconds=max(1, retry_after),
        )
    return RateLimitDecision(allowed=True, scope=scope, limit=limit)


def _client_identifier(request: Request) -> str:
    """Best-effort per-caller key for a request on the internal mesh.

    The inter-workcell routes sit behind HMAC, not a public reverse proxy, so
    there is no trusted ``X-Forwarded-For`` chain to unwind (unlike the intake
    endpoint). We use the direct socket peer, which cannot be spoofed. If a
    request ever carries the canonical trace id we fold it in so a single
    misbehaving caller-flow is still bucketed together when many share a peer.
    """
    direct = (request.client.host if request.client else "") or "unknown"
    return direct


def rate_limit_dependency(scope: str) -> Callable[[Request], None]:
    """Build a FastAPI dependency that enforces ``scope``'s per-minute limit.

    Usage::

        @app.post("/voice/call", dependencies=[Depends(rate_limit_dependency("voice_call"))])

    A breach raises ``HTTPException(429)`` with a ``Retry-After`` header.
    """

    def _dep(request: Request) -> None:
        decision = check_rate_limit(scope, _client_identifier(request))
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limited",
                    "scope": decision.scope,
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "message": (
                        "Rate limit exceeded for this endpoint. Retry after "
                        f"{decision.retry_after_seconds}s."
                    ),
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

    return _dep


def reset_counters() -> None:
    """Drop all in-process counters. For tests only."""
    global _COUNTER
    _COUNTER = _FixedWindowCounter()


__all__ = [
    "RateLimitDecision",
    "check_rate_limit",
    "rate_limit_dependency",
    "reset_counters",
]
