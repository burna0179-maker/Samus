"""Production pulse — idle-signal-driven campaign progression (2026-07-03).

OPERATOR DIRECTIVE: "instead of a fixed time allotment between campaign
ignition, tie it into the idle signal — if the agent is idle, it reasons
that it needs to progress a campaign in some way."

The reasoning already existed: ``run_idle_drive`` observes real production
activity (placed calls + sent emails from the ledgers), business hours, goal
pace, and affordability, and actuates one bounded governed portfolio pass.
The flaw was the TRIGGER: a 30-minute control-tick metronome SAMPLED the
idle signal (with a 45-minute "recently active" hold tuned for that
metronome), guaranteeing up to half an hour of dead air between passes —
downtime by design.

This loop WATCHES the signal instead: every ``SAMUS_PRODUCTION_PULSE_SEC``
(default 120s) it re-evaluates the same idle-drive reasoning with a pulse-
scale threshold ``SAMUS_PRODUCTION_IDLE_THRESHOLD_S`` (default 300s = 5 min
of quiet counts as idle). Result: while there is eligible work, production
flows continuously — pass, brief settle, pass — and when the pool / caps /
hours are exhausted the loop goes correctly quiet. Pacing is enforced by
the things that OWN pacing, not by dead air: per-pass caps, affordability
intensity, suppression + dedup, the send-ramp governor, the dialer's daily
cap + TCPA window, and the drive's own business-hours gate.

The 30-minute control tick keeps its periodic duties (entropy, portfolio
rebalance recommendations, auto-stake, stale-deal sweep) — its embedded
idle-drive stage simply finds production "recently active" and holds, so
the two loops compose instead of double-firing.

Wire-not-arm: ``SAMUS_PRODUCTION_PULSE_ENABLED`` (default OFF; the operator
arms it in compose/.env). Loop shape mirrors control_tick_task: settle
delay, fault-isolated iterations, idempotent start, best-effort stop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.gateway.production_pulse")

ENV_ENABLED = "SAMUS_PRODUCTION_PULSE_ENABLED"
ENV_PULSE_SEC = "SAMUS_PRODUCTION_PULSE_SEC"
ENV_IDLE_THRESHOLD_S = "SAMUS_PRODUCTION_IDLE_THRESHOLD_S"

_DEFAULT_PULSE_SEC = 120.0
_DEFAULT_IDLE_THRESHOLD_S = 300.0
_INITIAL_DELAY_SEC = 90.0  # let boot churn settle; control tick fires first


def _enabled() -> bool:
    return (os.environ.get(ENV_ENABLED, "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _pulse_sec() -> float:
    try:
        return max(30.0, float(os.environ.get(ENV_PULSE_SEC, "") or _DEFAULT_PULSE_SEC))
    except ValueError:
        return _DEFAULT_PULSE_SEC


def _idle_threshold_s() -> float:
    try:
        return max(
            60.0, float(os.environ.get(ENV_IDLE_THRESHOLD_S, "") or _DEFAULT_IDLE_THRESHOLD_S)
        )
    except ValueError:
        return _DEFAULT_IDLE_THRESHOLD_S


async def _production_pulse_loop(pulse: float, threshold: float) -> None:
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            from fastapi.concurrency import run_in_threadpool

            from backend.cash_engine.idle_production import run_idle_drive

            # The full observe->decide->produce reasoning, off the event loop
            # (ledger scans + a possible portfolio pass are blocking I/O).
            result = await run_in_threadpool(lambda: run_idle_drive(idle_threshold_s=threshold))
            # Log only signal: a pulse that held is silence, a pulse that
            # produced (or starved) is a line. The control-tick ledger keeps
            # the full periodic record; this loop is the fast path.
            if isinstance(result, dict) and (
                result.get("produced") or (result.get("self_supply") or {}).get("starved")
            ):
                act = result.get("actuation") or {}
                _LOG.info(
                    "production_pulse: produced=%s initiated=%s reason=%r self_supply=%s",
                    result.get("produced"),
                    act.get("initiated"),
                    result.get("reason"),
                    (result.get("self_supply") or {}).get("causes"),
                )
        except Exception:  # noqa: BLE001 — a pulse fault never kills the loop
            _LOG.exception("production_pulse iteration faulted; continuing")
        try:
            await asyncio.sleep(pulse)
        except asyncio.CancelledError:
            raise


async def start_production_pulse_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the pulse loop. Idempotent. Default OFF (operator arms)."""
    if not _enabled():
        _LOG.info("production pulse disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "production_pulse_task", None)
    if existing is not None and not existing.done():
        return existing
    pulse = _pulse_sec()
    threshold = _idle_threshold_s()
    task = asyncio.create_task(
        _production_pulse_loop(pulse, threshold),
        name="samus.production_pulse_loop",
    )
    app.state.production_pulse_task = task
    _LOG.info(
        "production pulse started (pulse=%.0fs idle_threshold=%.0fs)",
        pulse,
        threshold,
    )
    return task


async def stop_production_pulse_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "production_pulse_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.production_pulse_task = None
    _LOG.info("production pulse stopped")


__all__ = [
    "start_production_pulse_loop",
    "stop_production_pulse_loop",
    "ENV_ENABLED",
    "ENV_PULSE_SEC",
    "ENV_IDLE_THRESHOLD_S",
]
