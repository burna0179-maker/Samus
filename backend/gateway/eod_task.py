"""Gateway-internal end-of-day driver -- Samus closes out its own day.

Design rationale (operator directive 2026-07-08):
  The EOD three-way review was the LAST recurring Samus behaviour still driven
  by a Windows Task Scheduler entry (``Samus EOD Review`` -> Run-EndOfDayReview
  .ps1). External orchestration for a system that already reasons about its own
  state is the wrong seam: the host trigger died on svc_Samus execution policy,
  file ACLs, and CLM -- none of which exist inside the always-on container where
  the review actually runs. This module runs the same review as an in-container
  asyncio loop, the exact shape of ``morning_ritual_task`` / ``control_tick_task``.
  The stack keeps ONE boot-time scheduled task (``Launch_svc_Samus``); every
  recurring behaviour lives where the state is.

The review, once per business day, in the operator's evening window:
  * run ``run_end_of_day_review`` -- gathers the day's production, reconciles
    Samus (A) + OpenAI (B) + Framework/local (C) into a gameplan, persists
    ``eod_review_<date>.md`` + ``gameplan_<date>.json``, and routes corroborated
    findings into the guidance ledger.

Reasoning (the "when"): every ~5 min the loop asks three questions --
  (a) is the review armed at all?
  (b) is the Pacific hour inside the operator's end-of-day window (default
      18:00-23:59, matching the retired 18:30 host trigger)?
  (c) has today's review already been produced?  The EOD's OWN output file
      (``eod_review_<business_date>.md``) is the idempotency marker, so a manual
      ``/samus-eod`` earlier in the day correctly suppresses the auto-run -- no
      double review, no separate ledger to keep in sync.
Simple predicates today; the same seam morning_ritual leaves open for richer
signals (shift-complete detection, quiet-hours, operator activity).

Kill switch (default ON):
  * ``SAMUS_EOD_LOOP_ENABLED``     - master arm
Window knobs (Pacific 24h):
  * ``SAMUS_EOD_HOUR_PT``          - earliest fire hour, default 18
  * ``SAMUS_EOD_LATEST_HOUR_PT``   - latest fire hour (excl.), default 24
  * ``SAMUS_EOD_INTERVAL_SEC``     - poll cadence, default 300s
  * ``SAMUS_EOD_CONSULT_OPENAI``   - include OpenAI leg B, default ON
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.gateway.eod_task")

_DEFAULT_INTERVAL_SEC = 300.0
_INITIAL_DELAY_SEC = 160.0  # after control-tick(120)/planner(150) so events accrue

ENV_ENABLED = "SAMUS_EOD_LOOP_ENABLED"
ENV_HOUR = "SAMUS_EOD_HOUR_PT"
ENV_LATEST = "SAMUS_EOD_LATEST_HOUR_PT"
ENV_INTERVAL = "SAMUS_EOD_INTERVAL_SEC"
ENV_CONSULT = "SAMUS_EOD_CONSULT_OPENAI"


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


def _review_path(business_date: str):
    """Today's EOD review artifact -- the same file run_end_of_day_review writes
    (``storage.root()/cognition/eod_review_<date>.md``). Its existence is our
    once-per-day gate."""
    from backend.common import storage
    return storage.root() / "cognition" / f"eod_review_{business_date}.md"


def _already_ran_today(business_date: str) -> bool:
    """True iff today's EOD review artifact already exists.

    Fail-CLOSED (return True) on a read fault -- we would rather skip a duplicate
    review than risk re-ingesting guidance twice. Operator can rm the review
    file (or just run ``/samus-eod``) to force / already-satisfy a run.
    """
    try:
        return _review_path(business_date).is_file()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("eod gate read failed (%s); assuming already ran", exc)
        return True


def should_fire_now(business_date: str, hour_pt: int) -> tuple[bool, str]:
    """Reasoning core: should the EOD review fire on this tick?

    Returns ``(decision, reason)`` -- a decision plus a one-line narrative so the
    ops timeline is auditable. Pure + deterministic (the only I/O is the
    defensive review-file existence check) so it unit-tests cleanly.
    """
    if not _flag_on(ENV_ENABLED):
        return False, "master disabled"
    earliest = _int_env(ENV_HOUR, 18)
    latest = _int_env(ENV_LATEST, 24)
    if hour_pt < earliest:
        return False, f"before window ({hour_pt} < {earliest})"
    if hour_pt >= latest:
        return False, f"after window ({hour_pt} >= {latest})"
    if _already_ran_today(business_date):
        return False, "already ran today"
    return True, f"inside window {earliest}-{latest} PT and not yet run"


def _fire() -> dict[str, Any]:
    """Run one end-of-day review. Never raises -- a fault is logged and the loop
    retries next tick (and the review is itself fail-safe by design)."""
    try:
        from backend.cognitive.intelligence_cycle import run_end_of_day_review
        result = run_end_of_day_review(consult_openai=_flag_on(ENV_CONSULT))
        return {
            "ran": True,
            "review_id": result.get("review_id"),
            "leg_c_source": result.get("leg_c_source"),
            "method": (result.get("triangulation") or {}).get("method"),
            "ingested": result.get("ingested", 0),
            "eod_review_path": result.get("eod_review_path"),
            "error": result.get("error"),
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("eod review fire failed: %s", exc)
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}


async def _eod_loop(interval: float) -> None:
    """Poll every ``interval`` seconds; run the review when reasoning says yes.

    Structure mirrors ``morning_ritual_task._morning_ritual_loop`` so operators
    reading either module see one shape.
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
                _LOG.info("eod firing: %s (business_date=%s hour_pt=%d)",
                          reason, business_date, hour)
                result = _fire()
                _LOG.info(
                    "eod done: ran=%s leg_c=%s method=%s ingested=%s error=%s",
                    result.get("ran"), result.get("leg_c_source"),
                    result.get("method"), result.get("ingested"),
                    result.get("error"),
                )
            else:
                # INFO once per hour boundary so the timeline shows the loop is
                # alive without spamming every 5 min.
                if hour != getattr(_eod_loop, "_last_hour", None):
                    _LOG.info("eod skip: %s (business_date=%s hour_pt=%d)",
                              reason, business_date, hour)
                    _eod_loop._last_hour = hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            _LOG.exception("eod_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_eod_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container EOD loop. Idempotent. Default ON."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("eod loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "eod_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(_eod_loop(interval), name="samus.eod_loop")
    app.state.eod_task = task
    _LOG.info("eod loop started (interval=%.0fs)", interval)
    return task


async def stop_eod_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "eod_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 teardown swallows
        pass
    app.state.eod_task = None
    _LOG.info("eod loop stopped")


__all__ = [
    "start_eod_loop",
    "stop_eod_loop",
    "should_fire_now",
    "ENV_ENABLED",
    "ENV_HOUR",
    "ENV_LATEST",
    "ENV_INTERVAL",
    "ENV_CONSULT",
]
