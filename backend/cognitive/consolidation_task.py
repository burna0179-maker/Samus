"""In-container nightly consolidation timer (control_tick_task pattern).

An asyncio loop in the gateway lifespan that fires
:func:`backend.cognitive.consolidator.run_consolidation` once per night at
~02:00 Pacific, inside the always-on container -- independent of the host OS
and whether anyone is logged in (the same post-mortem lesson that created the
control-tick loop). ``scripts/Register-ConsolidationSchedule.ps1`` is the host
Task Scheduler belt-and-suspenders fallback.

Restart resilience (the 2026-07-07 fix)
---------------------------------------
The original loop was a bare ``sleep(seconds_until_next_fire); run`` one-shot
with NO persistence of the last run and NO missed-fire catch-up. The gateway
container is RECREATED on every deploy/rebuild (several times a day during
active development). Each recreate restarted the loop, which simply re-slept to
the *next* 02:00 -- so the consolidation only ever fired if the container stayed
up continuously across a 02:00, which basically never happens mid-development.
It failed SILENTLY (no error, the sleep target was just never reached), and the
whole overnight learning cycle went dark for ~7.6 days.

The fix keeps the trigger simple and additive (``run_consolidation`` itself is
untouched):

  * A durable **last-run-day marker** (``state/cognition/consolidation_last_run.json``,
    keyed ``YYYY-MM-DD``) records the day the consolidation last ran.
  * On loop START (boot) AND on every wake, :func:`run_if_due` asks "is a
    consolidation owed?" -- i.e. does the most recent scheduled fire day (today's
    HH:00 if we're at/after it, else yesterday's) post-date the marker? If owed,
    it runs IMMEDIATELY (missed-fire catch-up) and records the marker; otherwise
    it sleeps until the next fire.
  * The marker gates BOTH the boot catch-up and the same-day scheduled fire, so
    a day can consolidate **at most once** no matter how many times the container
    is recreated (strict idempotency -- running twice for a day is a no-op).

Timezone
--------
The container has no ``TZ`` set, so a naive ``02:00 local`` was actually 02:00
**UTC** = ~7pm Pacific -- an *active* evening hour for the operator, not the
quiet-night hour the nightly consolidation intends. This module now reasons in
the operator's business timezone (America/Los_Angeles) via
``us_timezones.state_to_timezone("CA")`` -- the SAME clock the sibling loops
(``morning_ritual_task`` / ``cold_dial_task``) already use -- so ``02:00`` means
02:00 Pacific, unambiguously. Falls back to naive ``datetime.now()`` if the tz
lookup ever faults (a wrong-by-hours window self-corrects on the next daily
catch-up; better than crashing the loop).

Default ON. Off-switch ``SAMUS_CONSOLIDATION_LOOP_ENABLED=0``; fire hour
``SAMUS_CONSOLIDATION_HOUR`` (Pacific, default 2). The consolidation itself is
synchronous ledger work, so it runs in a worker thread to keep the event loop
responsive. A run fault is logged and never kills the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.cognitive.consolidation_task")

ENV_ENABLED = "SAMUS_CONSOLIDATION_LOOP_ENABLED"
ENV_HOUR = "SAMUS_CONSOLIDATION_HOUR"

_DEFAULT_HOUR = 2
_MIN_SLEEP_SEC = 60.0  # never busy-spin even if the clock math degenerates
_INITIAL_DELAY_SEC = 90.0  # let boot churn settle before the first catch-up check

# Durable last-run-day marker. Lives under the writable state root (the same
# resolver the consolidator's own ledgers use) so it survives container
# recreation on the persistent data volume.
_MARKER_SUBDIR = "cognition"
_MARKER_NAME = "consolidation_last_run.json"


def _loop_enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _fire_hour() -> int:
    try:
        hour = int(os.environ.get(ENV_HOUR, "") or _DEFAULT_HOUR)
    except ValueError:
        return _DEFAULT_HOUR
    return hour if 0 <= hour <= 23 else _DEFAULT_HOUR


def _now_local() -> datetime:
    """Current time in the operator's business timezone (America/Los_Angeles).

    Mirrors ``cold_dial_task._now_local_pt`` / ``morning_ritual_task._now_pt`` so
    all the gateway loops reason on ONE clock. Falls back to a naive
    ``datetime.now()`` on any tz fault -- the daily catch-up self-corrects a
    wrong-by-hours window, which is far better than crashing the loop.
    """
    try:
        from backend.common.us_timezones import state_to_timezone

        return datetime.now(timezone.utc).astimezone(state_to_timezone("CA"))
    except Exception:  # noqa: BLE001 -- a tz fault must not kill scheduling
        return datetime.now()


def seconds_until_next_fire(now: datetime | None = None) -> float:
    """Seconds until the next HH:00 fire time (Pacific). Always > 0.

    ``now`` defaults to :func:`_now_local` (Pacific-aware); tests pin it with an
    explicit value. Arithmetic stays within a single datetime's tzinfo (aware or
    naive), so no naive/aware mixing.
    """
    current = now if now is not None else _now_local()
    target = current.replace(
        hour=_fire_hour(),
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= current:
        target += timedelta(days=1)
    return max(_MIN_SLEEP_SEC, (target - current).total_seconds())


# ---------------------------------------------------------------------------
# Pure decision core -- is a consolidation owed? (kept pure + injectable for
# unit tests; the impure marker read happens in :func:`run_if_due`)
# ---------------------------------------------------------------------------
def _scheduled_fire_day(now: datetime, hour: int) -> date:
    """Calendar date of the most recent scheduled HH:00 fire at/before ``now``.

    If ``now`` is at/after today's fire hour, today's fire has already passed
    (its day is today); otherwise the most recent fire was yesterday's HH:00.
    """
    if now.hour >= hour:
        return now.date()
    return now.date() - timedelta(days=1)


def is_consolidation_due(
    now: datetime,
    last_run_day: str | None,
    fire_hour: int | None = None,
) -> tuple[bool, str]:
    """Decide whether a consolidation is owed as of ``now``. Pure given inputs.

    DUE iff the most recent scheduled fire day (see :func:`_scheduled_fire_day`)
    is a day we have not yet consolidated -- ``last_run_day`` is None (never run)
    or strictly precedes it. Comparison is on ISO ``YYYY-MM-DD`` strings, which
    order lexicographically the same as by date. Fail-safe toward NOT running: a
    ``last_run_day`` at/after the owed day (including a future clock-skew value)
    reads as not-due, so we never double-consolidate.

    Returns ``(decision, reason)`` -- a one-line narrative so the ops timeline is
    easy to audit.
    """
    hour = _fire_hour() if fire_hour is None else fire_hour
    owed = _scheduled_fire_day(now, hour).isoformat()
    if last_run_day is None:
        return True, f"no consolidation on record; owed for {owed}"
    if last_run_day < owed:
        return True, f"last run {last_run_day} precedes owed fire day {owed}"
    return False, f"already consolidated for {last_run_day} (owed {owed})"


# ---------------------------------------------------------------------------
# Last-run-day marker -- durable, YYYY-MM-DD keyed
# ---------------------------------------------------------------------------
def _marker_path() -> Path:
    from backend.common.state_paths import state_path

    return state_path(_MARKER_SUBDIR, _MARKER_NAME)


def _valid_day(day: str) -> bool:
    try:
        date.fromisoformat(day)
        return True
    except ValueError:
        return False


def _read_last_run_day() -> Optional[str]:
    """Last day a consolidation ran (``YYYY-MM-DD``), or None if never / unreadable.

    Fail-OPEN to None (treat as "never run" => due). A missing marker is the
    normal first-boot / fresh-deploy case and MUST read as due; a corrupt marker
    is far rarer, and re-running once (which heals the marker) is far less harmful
    than the silent perpetual skip this loop exists to prevent. The re-run is
    itself bounded to at most once per fire cycle by the sleep cadence.
    """
    try:
        path = _marker_path()
        if not path.is_file():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
        day = row.get("last_run_day") if isinstance(row, dict) else None
        if isinstance(day, str) and _valid_day(day):
            return day
        return None
    except Exception as exc:  # noqa: BLE001 -- unreadable marker => treat as never-run
        _LOG.warning("consolidation marker read failed (%s); treating as never-run", exc)
        return None


def _write_last_run_day(day: str, *, ok: bool) -> None:
    """Persist the last-run-day marker. Best-effort; a write fault is logged.

    ``ok`` (whether every consolidation stage succeeded) is stored for
    observability only -- the DUE gate keys solely on the day, so a stage-level
    fault does NOT force a full re-run next cycle (which would duplicate the
    stages that DID succeed). The marker records "the nightly consolidation ran
    for this day", giving strict one-run-per-day idempotency.
    """
    try:
        from backend.common.dates import iso_now

        path = _marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "consolidation_last_run",
            "last_run_day": day,
            "ok": bool(ok),
            "recorded_at": iso_now(),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 -- a mark fault must not kill the loop
        _LOG.warning("consolidation marker write failed: %s", exc)


# ---------------------------------------------------------------------------
# The trigger -- decide, (catch up +) run, mark. Synchronous + injectable.
# ---------------------------------------------------------------------------
def run_if_due(
    now: datetime | None = None,
    *,
    runner: Optional[Callable[[str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Run the nightly consolidation iff one is owed as of ``now``; record the
    last-run-day marker on completion. Synchronous + injectable so the asyncio
    loop can offload it to a worker thread and tests can drive it deterministically.
    Never raises -- a fault degrades to a status dict so the loop keeps looping.

    The marker gates BOTH the boot catch-up and the same-day scheduled fire: once
    a day has run, every further call this day is a no-op. ``runner(day) ->
    result-dict`` defaults to
    :func:`backend.cognitive.consolidator.run_consolidation`; tests inject a fake.
    ``ran`` is True only when a consolidation actually executed this call.
    """
    try:
        current = now if now is not None else _now_local()
        last = _read_last_run_day()
        due, reason = is_consolidation_due(current, last)
        if not due:
            _LOG.info("consolidation not due: %s", reason)
            return {"ran": False, "reason": reason}

        owed_day = _scheduled_fire_day(current, _fire_hour()).isoformat()
        run = runner
        if run is None:
            from backend.cognitive.consolidator import run_consolidation

            run = run_consolidation
        # Pin the run to the owed fire day (not the container's naive "today"),
        # so the day windowed by distill/calibrate + the marker recorded below
        # agree with the fire we are satisfying -- including the narrow
        # before-fire-hour boot where the owed day is yesterday.
        result = run(owed_day) or {}
        day = str(result.get("day") or owed_day)
        _write_last_run_day(day, ok=bool(result.get("ok")))
        return {"ran": True, "reason": reason, "day": day, "ok": result.get("ok")}
    except Exception as exc:  # noqa: BLE001 -- a tick must never crash the loop
        _LOG.exception("consolidation run_if_due faulted")
        return {"ran": False, "reason": f"run_if_due-error: {exc}"}


# ---------------------------------------------------------------------------
# The asyncio loop + lifespan hooks
# ---------------------------------------------------------------------------
async def _consolidation_loop() -> None:
    """Boot catch-up + nightly scheduler in one gate.

    On boot (after a short settle) AND after every wake, ask "is a consolidation
    owed?" (:func:`run_if_due`) and run it immediately if so -- the missed-fire
    catch-up that makes the loop restart-resilient. Then sleep until the next
    scheduled fire. The last-run-day marker makes the boot catch-up and the
    same-day scheduled fire idempotent: at most ONE consolidation per day,
    however many times the container is recreated.
    """
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)  # let boot churn settle
    except asyncio.CancelledError:
        raise
    while True:
        try:
            # Consolidation is synchronous ledger work; run the whole
            # decide->run->mark tick in a worker thread so the gateway event
            # loop stays responsive while it runs.
            result = await asyncio.to_thread(run_if_due)
            if result.get("ran"):
                _LOG.info(
                    "consolidation_loop run: ok=%s day=%s (%s)",
                    result.get("ok"),
                    result.get("day"),
                    result.get("reason"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a run fault never kills the loop
            _LOG.exception("consolidation_loop tick faulted; continuing")
        try:
            await asyncio.sleep(seconds_until_next_fire())
        except asyncio.CancelledError:
            raise


async def start_consolidation_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the nightly consolidation loop. Idempotent. Default ON."""
    if not _loop_enabled():
        _LOG.info("consolidation loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "consolidation_task", None)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(
        _consolidation_loop(),
        name="samus.consolidation_loop",
    )
    app.state.consolidation_task = task
    _LOG.info(
        "consolidation loop started (fires daily at %02d:00 Pacific, with boot catch-up)",
        _fire_hour(),
    )
    return task


async def stop_consolidation_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "consolidation_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.consolidation_task = None
    _LOG.info("consolidation loop stopped")


__all__ = [
    "start_consolidation_loop",
    "stop_consolidation_loop",
    "seconds_until_next_fire",
    "is_consolidation_due",
    "run_if_due",
    "ENV_ENABLED",
    "ENV_HOUR",
]
