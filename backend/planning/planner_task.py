"""In-container planner timer — the planning loop self-drives.

HOTL Tranche 4. Same shape as ``backend/gateway/control_tick_task.py`` (the
revenue heartbeat): an asyncio loop in the gateway lifespan that runs one
planning cycle on an interval, INSIDE the always-on container, independent of
the host OS / Windows scheduler / whether anyone is logged in.

Each tick runs :func:`backend.planning.planner.run_planning_cycle`, which
ensures the goal tree + operational plans exist, evaluates every active daily
plan's assumptions against the unified event stream, and regenerates (Plan B)
any plan whose assumption is violated — escalating to the operator only when a
replan would breach budget posture or risk tier. The cycle is idempotent and
best-effort, so any cadence is safe; a fault is logged and never kills the loop.

Default-ON. Off-switch ``SAMUS_PLANNER_LOOP_ENABLED=0``; cadence
``SAMUS_PLANNER_INTERVAL_SEC`` (default 1800 = 30 min, matching the control
tick so a broken assumption is caught within half an hour).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.planning.planner_task")

_DEFAULT_INTERVAL_SEC = 1800.0
_INITIAL_DELAY_SEC = 150.0  # after control-tick's 120s, so events accrue first

ENV_ENABLED = "SAMUS_PLANNER_LOOP_ENABLED"
ENV_INTERVAL = "SAMUS_PLANNER_INTERVAL_SEC"


def _loop_enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _interval_sec() -> float:
    try:
        return float(os.environ.get(ENV_INTERVAL, "") or _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


async def _planner_loop(interval: float) -> None:
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            from backend.planning.planner import run_planning_cycle

            result = run_planning_cycle()
            _LOG.info(
                "planner_loop tick: ok=%s violations=%s replanned=%s escalated=%s",
                result.get("ok") if isinstance(result, dict) else "?",
                result.get("violations") if isinstance(result, dict) else "?",
                len(result.get("replanned", [])) if isinstance(result, dict) else "?",
                len(result.get("escalated", [])) if isinstance(result, dict) else "?",
            )
        except Exception:  # noqa: BLE001 — a tick fault never kills the loop
            _LOG.exception("planner_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_planner_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container planner loop. Idempotent. Default ON."""
    if not _loop_enabled():
        _LOG.info("planner loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "planner_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _interval_sec()
    task = asyncio.create_task(_planner_loop(interval), name="samus.planner_loop")
    app.state.planner_task = task
    _LOG.info("planner loop started (interval=%.0fs)", interval)
    return task


async def stop_planner_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "planner_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.planner_task = None
    _LOG.info("planner loop stopped")


__all__ = [
    "start_planner_loop",
    "stop_planner_loop",
    "ENV_ENABLED",
    "ENV_INTERVAL",
]
