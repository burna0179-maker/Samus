"""Gateway-internal control-tick driver — the revenue loop self-drives.

2026-06-13 post-mortem (Samus's first autonomous day): the stack-level
control tick — the ONLY in-container producer that walks staked deals through
the cash engine — was driven by a Windows Task Scheduler entry. It never ran:
the task was registered ``LogonType=Interactive`` (needs the operator logged
on) and lived in the host process anyway, while the cash-engine state + SQS
queue live INSIDE the gateway container. Result: a fully-staked deal (Kelly
Zimmerman) sat un-walked for 16 hours and zero revenue actions fired.

The fix is the same shape the relief forwarder + governance protocol-layer
already use: an asyncio loop in the gateway lifespan that runs the tick on an
interval, INSIDE the always-on container, independent of the host OS, the
Windows scheduler, and whether anyone is logged in. The Windows scheduled
task can stay as a belt-and-suspenders manual trigger, but the autonomous
revenue loop no longer depends on it.

Each tick runs :func:`backend.gateway.control_tick.run_control_tick`, which
already performs entropy scan + portfolio rebalance + the CRM stale sweep
(stale-deal alert + staked-escalation re-walk + cash-engine enqueue). The
tick is idempotent and best-effort, so any cadence is safe; a tick fault is
logged and never kills the loop.

Default-ON (this is the revenue heartbeat). Off-switch
``SAMUS_CONTROL_TICK_LOOP_ENABLED=0``; cadence
``SAMUS_CONTROL_TICK_INTERVAL_SEC`` (default 1800 = 30 min, so a freshly
staked deal walks within half an hour without waiting for any daily job).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.gateway.control_tick_task")

_DEFAULT_INTERVAL_SEC = 1800.0
_INITIAL_DELAY_SEC = 120.0  # let boot churn settle before the first tick

ENV_ENABLED = "SAMUS_CONTROL_TICK_LOOP_ENABLED"
ENV_INTERVAL = "SAMUS_CONTROL_TICK_INTERVAL_SEC"


def _loop_enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _interval_sec() -> float:
    try:
        return float(os.environ.get(ENV_INTERVAL, "") or _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


async def _control_tick_loop(interval: float) -> None:
    # Run once after a short settle delay, then on the cadence. Deferred
    # import keeps the gateway import graph light and avoids a hard
    # import-time coupling to the workcell modules the tick touches.
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            from backend.gateway.control_tick import run_control_tick

            result = run_control_tick()
            rec = result.get("recommendations", {}) if isinstance(result, dict) else {}
            _LOG.info(
                "control_tick_loop tick: ok=%s stale_deals=%s",
                result.get("ok") if isinstance(result, dict) else "?",
                rec.get("stale_deals"),
            )
        except Exception:  # noqa: BLE001 — a tick fault never kills the loop
            _LOG.exception("control_tick_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_control_tick_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container control-tick loop. Idempotent. Default ON."""
    if not _loop_enabled():
        _LOG.info("control_tick loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "control_tick_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _interval_sec()
    task = asyncio.create_task(
        _control_tick_loop(interval),
        name="samus.control_tick_loop",
    )
    app.state.control_tick_task = task
    _LOG.info("control_tick loop started (interval=%.0fs)", interval)
    return task


async def stop_control_tick_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "control_tick_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.control_tick_task = None
    _LOG.info("control_tick loop stopped")


__all__ = [
    "start_control_tick_loop",
    "stop_control_tick_loop",
    "ENV_ENABLED",
    "ENV_INTERVAL",
]
