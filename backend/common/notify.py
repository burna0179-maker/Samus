"""Shared operator push channel for security / anomaly signals.

This is the fleet's real-time alerting seam — the #1 blue-team gap was that
strong detection PRIMITIVES exist (hash-chained audit ledger, HMAC replay
protection, health observation) but every signal was log-only or pull-only:
nothing PUSHED to a human in real time. :func:`notify_operator` closes that
gap by promoting the Discord webhook that :mod:`backend.morning_send` already
uses for the daily brief into a shared, fail-soft notifier that security /
anomaly call sites can fire from anywhere.

Design contract (all four are load-bearing — a notifier that can break the
caller is worse than no notifier):

* **Same channel, no hardcoded URL.** The webhook URL is resolved from the
  SAME env var ``morning_send`` reads (``SAMUS_BRIEF_DISCORD_WEBHOOK``). No
  URL is ever embedded in source. If it is unset the notifier is a no-op that
  returns ``False`` (logged at debug) — a stack with no webhook configured
  runs unchanged.
* **Fail-soft, never raises.** Every path is wrapped; a transport error, a
  4xx from Discord, a bad URL, or an unexpected exception all return ``False``.
  A notify failure must never propagate into an HTTP request handler or a
  background job.
* **Rate-limit / dedup.** A short-TTL in-process cache keyed by ``dedup_key``
  suppresses repeats so a LOOPING condition (a health-check flapping, a burst
  of forged requests) pages the operator once, not every tick. No external
  dependency — a plain dict + a lock.
* **Severity shaping.** ``info | warning | critical`` selects the emoji +
  prefix so the operator can triage at a glance in the channel.

Usage from anywhere in ``backend`` (import lazily at the call site to avoid
import cycles with settings/config):

    from backend.common.notify import notify_operator
    notify_operator(
        "Ledger tamper",
        "canonical chain break on open",
        severity="critical",
        dedup_key="ledger_tamper",
    )

Usage from a plain script running inside a container: the backend package is
importable when ``PYTHONPATH=/opt/samus`` (the in-container project root), so
``from backend.common.notify import notify_operator`` works from any
``scripts/*.py`` invoked as e.g. ``PYTHONPATH=/opt/samus python
scripts/checkin_send_helper.py``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Final

import httpx

_LOG = logging.getLogger("samus.notify")

# Same env var backend.morning_send resolves the Discord webhook from. Kept as
# a named constant so the coupling is explicit and greppable — do NOT hardcode
# a URL anywhere in this module.
_WEBHOOK_ENV_VAR: Final[str] = "SAMUS_BRIEF_DISCORD_WEBHOOK"

_HTTP_TIMEOUT: Final[float] = 10.0

# Dedup / rate-limit window. A repeated notify with the same dedup_key within
# this many seconds is suppressed (returns True — the operator was already
# paged for that condition). Tunable via env for ops without a redeploy.
_DEFAULT_DEDUP_TTL_SEC: Final[float] = 300.0

# severity -> (emoji, UPPER label) for the message prefix.
_SEVERITY_STYLE: Final[dict[str, tuple[str, str]]] = {
    "info": ("\N{LARGE BLUE CIRCLE}", "INFO"),
    "warning": ("\N{WARNING SIGN}", "WARNING"),
    "critical": ("\N{LARGE RED CIRCLE}", "CRITICAL"),
}

# In-process dedup cache: dedup_key -> monotonic timestamp of last send.
_dedup_lock = threading.Lock()
_dedup_seen: dict[str, float] = {}


def _dedup_ttl() -> float:
    """Resolve the dedup TTL (seconds), env-overridable, defensive on parse."""
    raw = os.getenv("SAMUS_NOTIFY_DEDUP_TTL_SEC")
    if not raw:
        return _DEFAULT_DEDUP_TTL_SEC
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DEDUP_TTL_SEC
    return val if val > 0 else _DEFAULT_DEDUP_TTL_SEC


def _resolve_webhook_url() -> str:
    """Resolve the Discord webhook URL from the same source as morning_send.

    Returns an empty string when unset — the caller treats that as "no channel
    configured" and no-ops.
    """
    return (os.getenv(_WEBHOOK_ENV_VAR) or "").strip()


def _should_send(dedup_key: str | None, now: float, ttl: float) -> bool:
    """Return True if a message with ``dedup_key`` may be sent now.

    A ``None``/empty dedup_key is never suppressed (every call sends). A key
    seen within ``ttl`` seconds is suppressed. On a send-eligible key the cache
    is updated so the NEXT call within the window is suppressed. Also prunes
    stale entries opportunistically so the cache can't grow without bound.
    """
    if not dedup_key:
        return True
    with _dedup_lock:
        last = _dedup_seen.get(dedup_key)
        if last is not None and (now - last) < ttl:
            return False
        _dedup_seen[dedup_key] = now
        # Opportunistic prune of expired keys (bounded work; keeps the dict
        # from accumulating one-shot keys forever).
        if len(_dedup_seen) > 256:
            expired = [k for k, t in _dedup_seen.items() if (now - t) >= ttl]
            for k in expired:
                _dedup_seen.pop(k, None)
        return True


def _shape_content(title: str, message: str, severity: str) -> str:
    """Compose the Discord ``content`` string with a severity prefix.

    Discord caps content at 2000 chars; we truncate defensively well under.
    """
    emoji, label = _SEVERITY_STYLE.get(severity, _SEVERITY_STYLE["warning"])
    title = (title or "").strip() or "(untitled alert)"
    message = (message or "").strip()
    content = f"{emoji} **[{label}] {title}**"
    if message:
        content += f"\n{message}"
    if len(content) > 1900:
        content = content[:1900] + "\N{HORIZONTAL ELLIPSIS}"
    return content


def notify_operator(
    title: str,
    message: str,
    *,
    severity: str = "warning",
    dedup_key: str | None = None,
) -> bool:
    """Push a concise operator alert to the shared Discord webhook.

    This is the shared security / anomaly push channel. It reuses the webhook
    URL resolution of :mod:`backend.morning_send` (env ``SAMUS_BRIEF_DISCORD_
    WEBHOOK``) — no URL is hardcoded.

    Args:
        title: short headline (e.g. ``"Ledger tamper"``).
        message: one or two sentence detail line.
        severity: ``info`` | ``warning`` | ``critical`` — shapes the emoji /
            prefix. An unknown value degrades to ``warning`` styling.
        dedup_key: optional key; a repeat with the same key inside the TTL
            window (default 300s) is suppressed so a looping condition doesn't
            spam the channel. Pass a stable key per logical condition (e.g.
            ``"ledger_tamper"``, ``f"authfail:{caller}"``).

    Returns:
        ``True`` if the alert was posted (or intentionally suppressed as a
        dedup within the window — the operator is already aware); ``False`` if
        no webhook is configured or the POST failed.

    Never raises — a notify failure must never break the caller.
    """
    try:
        webhook_url = _resolve_webhook_url()
        if not webhook_url:
            _LOG.debug(
                "notify_operator: no webhook configured (%s unset); skipping",
                _WEBHOOK_ENV_VAR,
            )
            return False
        if not webhook_url.startswith(("http://", "https://")):
            _LOG.debug("notify_operator: webhook URL not http(s); skipping")
            return False

        now = time.monotonic()
        if not _should_send(dedup_key, now, _dedup_ttl()):
            _LOG.debug(
                "notify_operator: suppressed duplicate (dedup_key=%s within TTL)",
                dedup_key,
            )
            # Suppressed-as-dedup counts as success: the operator was already
            # paged for this condition; returning True lets callers treat the
            # alert as "delivered" and not retry/escalate.
            return True

        content = _shape_content(title, message, severity)
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(webhook_url, json={"content": content})
        if response.status_code >= 400:
            _LOG.warning(
                "notify_operator: discord returned http_%s", response.status_code,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - alerting must NEVER break the caller
        _LOG.debug("notify_operator: swallowed error: %s", exc)
        return False


def _reset_dedup_cache() -> None:
    """Test helper — clear the in-process dedup cache."""
    with _dedup_lock:
        _dedup_seen.clear()
