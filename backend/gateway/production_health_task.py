"""Gateway-internal production-health tripwire -- Samus watches itself.

Design rationale (operator directive 2026-07-07):
  The host-side ``Samus Production Health`` Windows scheduled task fires
  ``scripts/Run-ProductionHealth.ps1`` every ~15 minutes (96x/day), which
  wraps ``python -m backend.observability.production_health_notify``. That
  wrapper is state-change throttled: a persistent failure emails ONCE, a
  recovery emails once, and repeats are silent. The tripwire is essential
  (it caught a silent inbox-poll outage during the 2026-07-06 armed day),
  but it lives INSIDE the container -- the checks read the same artifacts
  the gateway writes -- so pinning it to Windows Task Scheduler is another
  external orchestrator on a system that already reasons about its state.

This module is the same shape as ``control_tick_task`` +
``morning_ritual_task``: an asyncio loop bound to the gateway lifespan
that invokes :func:`backend.observability.production_health_notify.dispatch`
on a cadence (default 900s = 15 min, matching the host task's cadence).

Dedup + rate-limit stay where they belong: the underlying ``dispatch``
already reads/writes the alert-state ledger at
``<artifact_root>/observability/production_health_alert_state.json`` and
only sends on state change. This loop is a pure driver -- it does not
add a second cooldown layer, does not compose emails, does not read the
health checks itself. If ``dispatch`` says "unchanged", the tick is a
no-op; if it says "alerted" / "recovered" / "no_recipient" /
"send_failed", we log and move on. A tick fault is logged and never
kills the loop, matching the sibling drivers.

Kill switch (default ON):
  * ``SAMUS_PRODUCTION_HEALTH_LOOP_ENABLED`` -- master arm

Cadence knob:
  * ``SAMUS_PRODUCTION_HEALTH_INTERVAL_SEC`` -- default 900s (15 min)

The Windows scheduled task can stay as belt-and-suspenders (dedup lives
in the ledger the notifier owns, so a duplicate driver at most produces
a duplicate 'unchanged' log line), but the autonomous tripwire no longer
depends on host-side scheduling or an interactive login.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.gateway.production_health_task")

_DEFAULT_INTERVAL_SEC = 900.0  # 15 min, matches the host task's cadence.
_INITIAL_DELAY_SEC = 120.0  # let boot churn settle before the first check.

ENV_ENABLED = "SAMUS_PRODUCTION_HEALTH_LOOP_ENABLED"
ENV_INTERVAL = "SAMUS_PRODUCTION_HEALTH_LOOP_INTERVAL_SEC"
ENV_EMAIL_TO = "SAMUS_BRIEF_EMAIL_TO"

# The host script defaults the alert recipient to the operator's inbox; the
# compose stack does not set SAMUS_BRIEF_EMAIL_TO (that was the ps1 wrapper's
# job). Mirror the morning-ritual loop: default it here so the loop is
# self-sufficient inside the container.
_DEFAULT_EMAIL_TO = ""  # env SAMUS_MORNING_EMAIL_TO / SAMUS_HEALTH_EMAIL_TO required


def _loop_enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _interval_sec() -> float:
    try:
        return float(os.environ.get(ENV_INTERVAL, "") or _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


async def _production_health_loop(interval: float) -> None:
    # Deferred import keeps the gateway import graph light and avoids a hard
    # import-time coupling to the observability workcell modules.
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            from backend.observability.production_health_notify import dispatch

            result = dispatch()
            action = result.get("action") if isinstance(result, dict) else "?"
            fp = result.get("fingerprint", "") if isinstance(result, dict) else ""
            # 'unchanged' is the steady-state -- log DEBUG so a healthy stack
            # does not spam INFO every 15 min. State changes and faults log
            # at INFO/WARNING so the ops timeline shows them.
            if action == "unchanged":
                _LOG.debug("production_health tick: unchanged (fp=%s)", fp)
            elif action in ("alerted", "recovered"):
                _LOG.info(
                    "production_health tick: %s (fp=%s to=%s)",
                    action, fp, result.get("to"),
                )
            elif action == "send_failed":
                _LOG.warning(
                    "production_health tick: send_failed (fp=%s error=%s)",
                    fp, result.get("error"),
                )
            else:
                # disabled | no_recipient | anything unexpected
                _LOG.info("production_health tick: %s (fp=%s)", action, fp)
        except Exception:  # noqa: BLE001 -- a tick fault never kills the loop
            _LOG.exception("production_health_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_production_health_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container production-health loop. Idempotent. Default ON."""
    """Schedule the in-container production-health tripwire. Idempotent. Default ON."""
    if not _loop_enabled():
        _LOG.info("production_health loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "production_health_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _interval_sec()
    task = asyncio.create_task(
        _production_health_loop(interval), name="samus.production_health_loop",
    )
    app.state.production_health_task = task
    _LOG.info("production_health loop started (interval=%.0fs)", interval)
    return task


async def stop_production_health_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "production_health_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 -- teardown swallows
        pass
    app.state.production_health_task = None
    _LOG.info("production_health loop stopped")


__all__ = [
    "start_production_health_loop",
    "stop_production_health_loop",
    "ENV_ENABLED",
    "ENV_INTERVAL",
    "ENV_EMAIL_TO",
]
