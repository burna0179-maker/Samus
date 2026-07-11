"""Gateway-internal morning ritual driver -- Samus schedules its own day.

Design rationale (operator directive 2026-07-06):
  Windows Task Scheduler entries for time-based rituals are external
  orchestration for a system that already reasons about its own state.
  Instead of registering a Samus_Morning_Brief scheduled task, this module
  runs the same ritual as an in-container asyncio loop -- same shape as
  ``control_tick_task`` and ``production_pulse_task``. The stack has ONE
  boot-time scheduled task (``Launch_svc_Samus``); every recurring
  behaviour lives inside the always-on container where the state is.

The ritual, once per business day, in one atomic pass:
  1. Attest -- ``run_pre_shift_briefing`` stamps the ADR-018 attestation
     for today's business (Pacific) date, unlocking governed autonomous
     B2B live dial for the rest of the day.
  2. Compose -- render the operator brief (finance + sales + gates +
     readiness) via ``morning.render_briefing``.
  3. Send -- ``morning_send.main`` dispatches to email + Discord +
     Telegram, whichever are configured in the env.
  4. Ledger -- stamp a per-business-day marker so the next tick within
     the same day is a no-op.

Reasoning (the "when"): every ~5 min the loop asks Samus to answer three
questions -- (a) is today's ritual done? (b) is the Pacific hour inside
the operator's morning window (default 07:00-09:00)? (c) is the ritual
armed at all? Simple predicates today; leaves room for richer signals
(operator activity, market conditions, on-call pager) without disturbing
the scheduling seam.

Kill switches (composable, all default ON):
  * ``SAMUS_MORNING_RITUAL_ENABLED``          - master arm
  * ``SAMUS_MORNING_RITUAL_ATTEST_ENABLED``   - skip step 1 (manual attest)
  * ``SAMUS_MORNING_RITUAL_SEND_ENABLED``     - skip steps 2+3 (dry-run)

Window knobs (Pacific 24h):
  * ``SAMUS_MORNING_RITUAL_HOUR_PT``          - earliest fire hour, default 7
  * ``SAMUS_MORNING_RITUAL_LATEST_HOUR_PT``   - latest fire hour (excl.), default 10
  * ``SAMUS_MORNING_RITUAL_INTERVAL_SEC``     - poll cadence, default 300s

Ledger:
  ``<artifact_root>/cognition/morning_ritual_fired_<yyyy-mm-dd>.json``
  written by :func:`_mark_fired`; consulted by :func:`_already_fired_today`.
  Fail-open on read (assume fired to avoid double-send on a corrupt file)
  and fail-open on write (a mark failure logs but doesn't retry the send).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.gateway.morning_ritual_task")

_DEFAULT_INTERVAL_SEC = 300.0
_INITIAL_DELAY_SEC = 90.0  # let boot churn settle

ENV_ENABLED = "SAMUS_MORNING_RITUAL_ENABLED"
ENV_ATTEST = "SAMUS_MORNING_RITUAL_ATTEST_ENABLED"
ENV_SEND = "SAMUS_MORNING_RITUAL_SEND_ENABLED"
ENV_HOUR = "SAMUS_MORNING_RITUAL_HOUR_PT"
ENV_LATEST = "SAMUS_MORNING_RITUAL_LATEST_HOUR_PT"
ENV_INTERVAL = "SAMUS_MORNING_RITUAL_INTERVAL_SEC"
ENV_EMAIL_TO = "SAMUS_BRIEF_EMAIL_TO"
_DEFAULT_EMAIL_TO = ""  # env SAMUS_MORNING_EMAIL_TO / SAMUS_HEALTH_EMAIL_TO required


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


def _now_pt():
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


def _ledger_path(business_date: str):
    """Per-day fire marker. Same dir as attestation + primer for locality."""
    from backend.common import storage
    d = storage.root() / "cognition"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"morning_ritual_fired_{business_date}.json"


def _already_fired_today(business_date: str) -> bool:
    """True iff a fire record exists for the given business date.

    Fail-CLOSED (return True) on a read fault -- we would rather skip a
    send today than double-send. Operator can rm the ledger file to force
    a fire.
    """
    try:
        path = _ledger_path(business_date)
        if not path.is_file():
            return False
        row = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(row, dict) and row.get("business_date") == business_date
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("morning_ritual ledger read failed (%s); assuming fired", exc)
        return True


def _mark_fired(business_date: str, result: dict[str, Any]) -> None:
    """Stamp the fire record. Best-effort; a write fault doesn't retry."""
    try:
        from backend.common.dates import iso_now
        payload = {
            "kind": "morning_ritual_fired",
            "business_date": business_date,
            "fired_at": iso_now(),
            "briefing_id": result.get("briefing_id"),
            "ingested": result.get("ingested"),
            "send_rc": result.get("send_rc"),
        }
        _ledger_path(business_date).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("morning_ritual mark_fired failed: %s", exc)


def should_fire_now(business_date: str, hour_pt: int) -> tuple[bool, str]:
    """Reasoning core: should the ritual fire on this tick?

    Returns ``(decision, reason)`` -- a decision plus a one-line narrative so
    logs are easy to audit. Kept pure + deterministic for unit tests; the
    only I/O is the fire-ledger read, which is defensive.
    """
    if not _flag_on(ENV_ENABLED):
        return False, "master disabled"
    earliest = _int_env(ENV_HOUR, 7)
    latest = _int_env(ENV_LATEST, 10)
    if hour_pt < earliest:
        return False, f"before window ({hour_pt} < {earliest})"
    if hour_pt >= latest:
        return False, f"after window ({hour_pt} >= {latest})"
    if _already_fired_today(business_date):
        return False, "already fired today"
    return True, f"inside window {earliest}-{latest} PT and not fired"


def _do_attest() -> dict[str, Any]:
    """Run the pre-shift briefing (stamps ADR-018 attestation + ingests
    guidance). Fail-open on any fault -- the send still proceeds so the
    operator gets SOME morning signal even if the LLM/attestation faults.
    """
    if not _flag_on(ENV_ATTEST):
        return {"attested": False, "skipped": True}
    try:
        from backend.cognitive.intelligence_cycle import run_pre_shift_briefing
        result = run_pre_shift_briefing()
        return {
            "attested": True,
            "briefing_id": result.get("briefing_id"),
            "ingested": result.get("ingested", 0),
            "attestation_path": result.get("attestation_path"),
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("morning_ritual attest failed: %s", exc)
        return {"attested": False, "error": f"{type(exc).__name__}: {exc}"}


def _do_send() -> dict[str, Any]:
    """Render + dispatch the operator brief via ``morning_send.main``.
    Reads SendGrid + Discord + Telegram env vars the workcell already has;
    defaults the recipient to the operator's inbox when unset.
    """
    if not _flag_on(ENV_SEND):
        return {"sent": False, "skipped": True}
    if not (os.environ.get(ENV_EMAIL_TO) or "").strip():
        # morning_send treats an empty SAMUS_BRIEF_EMAIL_TO as "channel off",
        # so default it here to the operator's inbox -- the compose stack
        # doesn't set it (it was previously the Send-Morning.ps1 host wrapper's
        # job). Kept scoped to this call so we don't leak into the loop env.
        os.environ[ENV_EMAIL_TO] = _DEFAULT_EMAIL_TO
    try:
        from backend import morning_send
        rc = morning_send.main([])
        return {"sent": True, "send_rc": int(rc)}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("morning_ritual send failed: %s", exc)
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}


def _fire() -> dict[str, Any]:
    """One end-to-end pass: attest -> send -> stamp ledger. Returns a
    merged result the loop + logs read from. Never raises (each half
    fail-open).

    The ledger stamp is folded in here so an out-of-band manual invocation
    (``docker exec ... _fire()``) is atomic with the automatic tick's
    marker: the next tick sees "already fired today" and no-ops. Without
    this, a manual fire before the 07:00 PT window would leave the ledger
    empty and the auto-tick would double-send.
    """
    attest_out = _do_attest()
    send_out = _do_send()
    result = {**attest_out, **send_out}
    business_date, _ = _now_pt()
    _mark_fired(business_date, result)
    return result


async def _morning_ritual_loop(interval: float) -> None:
    """Poll every ``interval`` seconds; fire when reasoning says yes.

    Structure mirrors ``control_tick_task._control_tick_loop`` so operators
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
                _LOG.info("morning_ritual firing: %s (business_date=%s hour_pt=%d)",
                          reason, business_date, hour)
                result = _fire()
                _LOG.info(
                    "morning_ritual fired: attested=%s sent=%s briefing_id=%s send_rc=%s",
                    result.get("attested"), result.get("sent"),
                    result.get("briefing_id"), result.get("send_rc"),
                )
            else:
                # Log at INFO once per hour boundary so the ops timeline shows
                # the ritual is alive without spamming every 5 min.
                if hour != getattr(_morning_ritual_loop, "_last_hour", None):
                    _LOG.info("morning_ritual skip: %s (business_date=%s hour_pt=%d)",
                              reason, business_date, hour)
                    _morning_ritual_loop._last_hour = hour  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            _LOG.exception("morning_ritual_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_morning_ritual_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container morning ritual loop. Idempotent. Default ON."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("morning_ritual loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "morning_ritual_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(
        _morning_ritual_loop(interval), name="samus.morning_ritual_loop",
    )
    app.state.morning_ritual_task = task
    _LOG.info("morning_ritual loop started (interval=%.0fs)", interval)
    return task


async def stop_morning_ritual_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "morning_ritual_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 teardown swallows
        pass
    app.state.morning_ritual_task = None
    _LOG.info("morning_ritual loop stopped")


__all__ = [
    "start_morning_ritual_loop",
    "stop_morning_ritual_loop",
    "should_fire_now",
    "ENV_ENABLED",
    "ENV_ATTEST",
    "ENV_SEND",
    "ENV_HOUR",
    "ENV_LATEST",
    "ENV_INTERVAL",
]
