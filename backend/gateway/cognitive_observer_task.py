"""Gateway-internal cognitive-observer driver -- Samus grades its own forecasts.

Design rationale (operator directive 2026-07-08):
  Every autonomous choice already mints a ``DecisionRecord`` with a
  ``confidence`` and an ``expected_outcome`` on the unified business-event
  stream, but nothing SCORES those forecasts against the outcome events that
  land later on the same correlation ids. Two P1 blind spots follow directly:
    G1 -- confidence calibration is OPEN (Samus can be systematically over/
          under-confident and never know it), and
    G2 -- loop-completion is UNMEASURED (nothing detects reasoning loops that
          fire a decision and then never reach reality -- exactly the EOD-dark
          silent-degradation failure mode).
  Both are read-only over telemetry, so both live inside the container next to
  the state they observe -- same shape as the EOD driver, same env style,
  same wiring in app.py's ``_gateway_lifespan``.

The observer, once per business day, in the operator's overnight window:
  * run :func:`backend.cognitive.calibration_diagnostic.write_calibration_report`
    -- reconciles the last N days of ``decision.made`` events against the
    downstream outcome events on their correlation ids, persists
    ``calibration_report_<date>.json`` under ``storage.root()/cognition/``.

Reasoning (the "when"): every ~10 min the loop asks three questions --
  (a) is the observer armed at all?
  (b) is the Pacific hour inside the operator's overnight window (default
      03:00-05:00 PT -- deliberately AFTER the 02:00 consolidator slot so the
      calibration read sees a settled event stream, and well before morning
      operations start)?
  (c) has today's report already been produced?  The diagnostic's OWN output
      file (``calibration_report_<business_date>.json``) is the idempotency
      marker, so a manual re-run earlier in the day correctly suppresses the
      auto-run.

Kill switch (default ON):
  * ``SAMUS_COGNITIVE_OBSERVER_LOOP_ENABLED`` - master arm
Window knobs (Pacific 24h):
  * ``SAMUS_COGNITIVE_OBSERVER_HOUR_PT``        - earliest fire hour, default 3
  * ``SAMUS_COGNITIVE_OBSERVER_LATEST_HOUR_PT`` - latest fire hour (excl.), default 5
  * ``SAMUS_COGNITIVE_OBSERVER_INTERVAL_SEC``   - poll cadence, default 600s
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.gateway.cognitive_observer_task")

_DEFAULT_INTERVAL_SEC = 600.0
_INITIAL_DELAY_SEC = 180.0  # after control-tick / planner / eod so events accrue

ENV_ENABLED = "SAMUS_COGNITIVE_OBSERVER_LOOP_ENABLED"
ENV_HOUR = "SAMUS_COGNITIVE_OBSERVER_HOUR_PT"
ENV_LATEST = "SAMUS_COGNITIVE_OBSERVER_LATEST_HOUR_PT"
ENV_INTERVAL = "SAMUS_COGNITIVE_OBSERVER_INTERVAL_SEC"


def _flag_on(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _now_pt() -> tuple[str, int]:
    """(business_date_iso, pacific_hour_int). Falls back to UTC on fault."""
    try:
        from datetime import datetime, timezone
        from backend.common.us_timezones import state_to_timezone

        now = datetime.now(timezone.utc).astimezone(state_to_timezone("CA"))
        return now.date().isoformat(), now.hour
    except Exception:  # noqa: BLE001
        from datetime import date, datetime

        now = datetime.utcnow()
        return date.today().isoformat(), now.hour


def _report_path(business_date: str):
    """Today's calibration report artifact -- the same file
    ``write_calibration_report`` writes
    (``storage.root()/cognition/calibration_report_<date>.json``). Its
    existence is our once-per-day gate."""
    from backend.common import storage

    return storage.root() / "cognition" / f"calibration_report_{business_date}.json"


def _already_ran_today(business_date: str) -> bool:
    """True iff today's calibration report artifact already exists.

    Fail-CLOSED (return True) on a read fault -- we would rather skip a
    duplicate diagnostic than double-write. Operator can rm the report file
    to force a re-run.
    """
    try:
        return _report_path(business_date).is_file()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("cognitive_observer gate read failed (%s); assuming already ran", exc)
        return True


def should_fire_now(business_date: str, hour_pt: int) -> tuple[bool, str]:
    """Reasoning core: should the calibration diagnostic fire on this tick?

    Returns ``(decision, reason)`` -- a decision plus a one-line narrative so
    the ops timeline is auditable. Pure + deterministic (the only I/O is the
    defensive report-file existence check) so it unit-tests cleanly.
    """
    if not _flag_on(ENV_ENABLED):
        return False, "master disabled"
    earliest = _int_env(ENV_HOUR, 3)
    latest = _int_env(ENV_LATEST, 5)
    if hour_pt < earliest:
        return False, f"before window ({hour_pt} < {earliest})"
    if hour_pt >= latest:
        return False, f"after window ({hour_pt} >= {latest})"
    if _already_ran_today(business_date):
        return False, "already ran today"
    return True, f"inside window {earliest}-{latest} PT and not yet run"


def _fire(business_date: str) -> dict[str, Any]:
    """Run one calibration diagnostic. Never raises -- a fault is logged and
    the loop retries next tick (and the diagnostic is itself fail-safe by
    design)."""
    try:
        from backend.cognitive.calibration_diagnostic import write_calibration_report

        path = write_calibration_report(business_date)
        # Read back a quick summary line for the ops log without re-running
        # the reconcile (the JSON is small).
        summary: dict[str, Any] = {"ran": True, "report_path": str(path)}
        try:
            import json

            with path.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
            overall = doc.get("overall") or {}
            summary["decisions"] = overall.get("decisions")
            summary["unscoreable"] = overall.get("unscoreable")
            summary["loop_completion_rate"] = overall.get("loop_completion_rate")
            summary["actors"] = len(doc.get("per_actor") or {})
        except Exception as exc:  # noqa: BLE001 -- summary is best-effort
            summary["summary_error"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("cognitive_observer fire failed: %s", exc)
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}


async def _cognitive_observer_loop(interval: float) -> None:
    """Poll every ``interval`` seconds; run the diagnostic when reasoning says yes.

    Structure mirrors ``eod_task._eod_loop`` so operators reading either module
    see one shape.
    """
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            business_date, hour = _now_pt()
            fire, reason = should_fire_now(business_date, hour)
            if fire:
                _LOG.info(
                    "cognitive_observer firing: %s (business_date=%s hour_pt=%d)",
                    reason,
                    business_date,
                    hour,
                )
                result = _fire(business_date)
                _LOG.info(
                    "cognitive_observer done: ran=%s decisions=%s unscoreable=%s "
                    "loop_completion_rate=%s actors=%s error=%s",
                    result.get("ran"),
                    result.get("decisions"),
                    result.get("unscoreable"),
                    result.get("loop_completion_rate"),
                    result.get("actors"),
                    result.get("error"),
                )
            else:
                if hour != getattr(_cognitive_observer_loop, "_last_hour", None):
                    _LOG.info(
                        "cognitive_observer skip: %s (business_date=%s hour_pt=%d)",
                        reason,
                        business_date,
                        hour,
                    )
                    _cognitive_observer_loop._last_hour = hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            _LOG.exception("cognitive_observer_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_cognitive_observer_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container cognitive-observer loop. Idempotent. Default ON."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("cognitive_observer loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "cognitive_observer_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(
        _cognitive_observer_loop(interval),
        name="samus.cognitive_observer_loop",
    )
    app.state.cognitive_observer_task = task
    _LOG.info("cognitive_observer loop started (interval=%.0fs)", interval)
    return task


async def stop_cognitive_observer_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "cognitive_observer_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 teardown swallows
        pass
    app.state.cognitive_observer_task = None
    _LOG.info("cognitive_observer loop stopped")


__all__ = [
    "start_cognitive_observer_loop",
    "stop_cognitive_observer_loop",
    "should_fire_now",
    "ENV_ENABLED",
    "ENV_HOUR",
    "ENV_LATEST",
    "ENV_INTERVAL",
]
