"""DynamoDB-backed fixed-window rate limiter for the public intake endpoint.

Why DynamoDB and not an in-process counter
-------------------------------------------
``POST /intake/onboarding`` runs on Cloud Run, which scales horizontally to
multiple stateless instances. A pure in-process ``dict`` counter only sees
the traffic that lands on one instance, so a flood spread across instances
would slip past every per-instance cap. DynamoDB's atomic ``ADD`` update
expression gives a counter that is *shared* across every instance — the same
table (``samus_idempotency``) the rest of the workcell already provisions, so
no parallel infrastructure is introduced.

Window model
------------
Fixed (not sliding) windows: each window is a discrete time bucket whose id
is ``floor(now / window_seconds)``. A counter row is keyed by
``(scope, identifier, bucket)`` and carries a DynamoDB TTL ``expires_at`` so
the table self-reaps stale rows. Fixed windows are chosen over sliding
windows deliberately — they need exactly one atomic increment per request
(no read-modify-write race), which is both cheaper and correct under
concurrency.

Fail-open contract
------------------
Every DynamoDB interaction is wrapped: if the backend errors (throttle, IAM
misconfig, network), the limiter returns *allow* and logs a warning. A
rate-limit backend hiccup must never block a legitimate onboarding lead —
losing a sale is worse than briefly tolerating abuse.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from backend.common import aws
from backend.common.config import get_settings


_LOG = logging.getLogger("samus.intake.rate_limit")

# Logical scopes — kept short because they are part of the DynamoDB partition
# key. ``ip`` counts one source IP; ``global`` counts every request regardless
# of source as a distributed-flood backstop.
_SCOPE_IP = "ip"
_SCOPE_GLOBAL = "global"

# Window lengths in seconds.
_MINUTE = 60
_HOUR = 3600


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a rate-limit check.

    ``allowed`` is the only field a caller must branch on. ``scope`` and
    ``limit`` describe which ceiling was breached (for the 429 detail / logs);
    ``backend_error`` is set when the limiter failed open so the breach (or
    non-breach) was not actually counted.
    """
    allowed: bool
    scope: str = ""
    limit: int = 0
    retry_after_seconds: int = 0
    backend_error: str | None = None


def _counter_table() -> Any:
    """The DynamoDB table backing the shared counters.

    Reuses ``samus_idempotency`` — it already exists in every environment,
    has a single string partition key, and holds short-lived dedup rows, so a
    TTL'd rate-limit counter is a natural fit. No new table to provision.
    """
    settings = get_settings()
    return aws.table(settings.ddb_idempotency_table, settings.aws_region)


def _bucket(now: float, window_seconds: int) -> int:
    """Discrete fixed-window bucket id for ``now``."""
    return int(now // window_seconds)


def _counter_key(scope: str, identifier: str, window_seconds: int, bucket: int) -> str:
    """Partition-key value for one (scope, identifier, window, bucket) counter.

    Namespaced under ``intake.ratelimit:`` so these rows never collide with
    the SQS dedup keys that ``samus_idempotency`` otherwise holds.
    """
    return f"intake.ratelimit:{scope}:{identifier}:{window_seconds}:{bucket}"


def _increment_and_get(key: str, window_seconds: int, now: float) -> int:
    """Atomically increment the counter for ``key`` and return the new value.

    Uses DynamoDB's ``ADD`` update expression — a single atomic operation, so
    concurrent requests across Cloud Run instances each see a correct,
    monotonically increasing count with no read-modify-write race. Also sets
    an ``expires_at`` TTL attribute so DynamoDB reaps the row once the window
    (plus a small grace) has elapsed.

    Raises on any DynamoDB error — the caller is responsible for the
    fail-open decision so it can record ``backend_error``.
    """
    settings = get_settings()
    grace = max(0, int(settings.intake_rate_limit_ttl_grace_seconds))
    expires_at = int(now) + window_seconds + grace
    resp = _counter_table().update_item(
        Key={"idempotency_key": key},
        UpdateExpression="ADD request_count :one SET expires_at = :exp",
        ExpressionAttributeValues={":one": 1, ":exp": expires_at},
        ReturnValues="UPDATED_NEW",
    )
    attrs = resp.get("Attributes") or {}
    # DynamoDB returns numeric attributes as Decimal; int() normalizes.
    return int(attrs.get("request_count") or 0)


def _check_window(
    scope: str,
    identifier: str,
    *,
    window_seconds: int,
    limit: int,
    now: float,
) -> RateLimitDecision:
    """Increment one (scope, window) counter and decide allow/deny.

    A non-positive ``limit`` disables that ceiling (always allowed). On a
    DynamoDB error the limiter fails OPEN: it returns ``allowed=True`` with
    ``backend_error`` set so the route can log the degradation.
    """
    if limit <= 0:
        return RateLimitDecision(allowed=True, scope=scope, limit=limit)
    bucket = _bucket(now, window_seconds)
    key = _counter_key(scope, identifier, window_seconds, bucket)
    try:
        count = _increment_and_get(key, window_seconds, now)
    except Exception as exc:  # noqa: BLE001 — fail OPEN on backend trouble
        _LOG.warning(
            "intake rate-limit backend error (scope=%s window=%ss): %s — failing open",
            scope, window_seconds, exc,
        )
        return RateLimitDecision(
            allowed=True, scope=scope, limit=limit,
            backend_error=f"rate_limit_backend_error: {exc}",
        )
    if count > limit:
        # Seconds until this fixed window rolls over.
        retry_after = window_seconds - int(now % window_seconds)
        _LOG.info(
            "intake rate-limit breach scope=%s id=%s window=%ss count=%d limit=%d",
            scope, identifier, window_seconds, count, limit,
        )
        return RateLimitDecision(
            allowed=False, scope=scope, limit=limit,
            retry_after_seconds=max(1, retry_after),
        )
    return RateLimitDecision(allowed=True, scope=scope, limit=limit)


def check_rate_limit(source_ip: str, *, now: float | None = None) -> RateLimitDecision:
    """Check every configured ceiling for one onboarding request.

    Order of checks (most-specific first): per-IP-minute, per-IP-hour, then
    the cross-IP global-hour backstop. The first breach short-circuits and is
    returned. When the limiter is disabled via settings, or all ceilings pass,
    an allowed decision is returned.

    Counters are still incremented for ceilings checked before a breach — a
    fixed-window limiter that breaches on the minute window has already
    recorded that request, which is the intended behaviour (the request
    counts whether or not it is ultimately served).
    """
    settings = get_settings()
    if not settings.intake_rate_limit_enabled:
        return RateLimitDecision(allowed=True)

    clock = time.time() if now is None else now
    # An unknown source IP collapses every anonymous caller onto one bucket.
    # That is the safe direction: it cannot be used to evade the limiter, only
    # to share a (stricter) common bucket.
    identifier = (source_ip or "unknown").strip() or "unknown"

    minute_decision = _check_window(
        _SCOPE_IP, identifier,
        window_seconds=_MINUTE,
        limit=settings.intake_rate_limit_per_minute,
        now=clock,
    )
    if not minute_decision.allowed:
        return minute_decision

    hour_decision = _check_window(
        _SCOPE_IP, identifier,
        window_seconds=_HOUR,
        limit=settings.intake_rate_limit_per_hour,
        now=clock,
    )
    if not hour_decision.allowed:
        return hour_decision

    global_decision = _check_window(
        _SCOPE_GLOBAL, _SCOPE_GLOBAL,
        window_seconds=_HOUR,
        limit=settings.intake_rate_limit_global_per_hour,
        now=clock,
    )
    if not global_decision.allowed:
        return global_decision

    # Surface any fail-open backend error from the last window checked so the
    # route can log that the limiter degraded; the request is still allowed.
    backend_error = (
        minute_decision.backend_error
        or hour_decision.backend_error
        or global_decision.backend_error
    )
    return RateLimitDecision(allowed=True, backend_error=backend_error)
