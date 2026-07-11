"""In-container periodic loop that runs the calendar-poller.

Mirrors :mod:`backend.intake.gmail_poll_task` — every ``ENV_INTERVAL_SEC``
seconds calls :func:`backend.intake.calendar_poller.poll_calendar_once`.
Started/stopped from the intake FastAPI app's lifespan hook.

Rationale (parity with the Gmail poller): a Windows Scheduled Task for
recurring behavior against always-on container state is a footgun the
fleet has already been bitten by. The loop lives inside the container.

Kill switches:

* ``SAMUS_CALENDAR_POLL_ENABLED``      — master arm, default ON
* ``SAMUS_CALENDAR_POLL_INTERVAL_SEC`` — poll cadence, default 300s (5 min)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.intake.calendar_poll_task")

_DEFAULT_INTERVAL_SEC = 300.0
_INITIAL_DELAY_SEC = 90.0  # let boot settle + gmail poll's first tick pass

ENV_ENABLED = "SAMUS_CALENDAR_POLL_ENABLED"
ENV_INTERVAL = "SAMUS_CALENDAR_POLL_INTERVAL_SEC"


def _flag_on(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def should_poll_now() -> tuple[bool, str]:
    if not _flag_on(ENV_ENABLED):
        return False, "master disabled"
    return True, "master enabled"


def _run_poll_pass() -> dict[str, Any]:
    try:
        from backend.intake.calendar_poller import poll_calendar_once
        result = poll_calendar_once()
        return {
            "enabled": bool(result.enabled),
            "fetched": int(result.fetched),
            "ingested": int(result.ingested),
            "scheduled": int(result.scheduled_emitted),
            "completed": int(result.completed_emitted),
            "already_seen": int(result.already_seen),
            "samus_owned": int(result.skipped_samus_owned),
            "connect_error": result.connect_error or "",
        }
    except Exception as exc:  # noqa: BLE001 — loop must never die
        _LOG.exception("calendar poll pass raised (fail-soft): %s", exc)
        return {
            "enabled": False, "fetched": 0, "ingested": 0,
            "scheduled": 0, "completed": 0, "already_seen": 0,
            "samus_owned": 0,
            "connect_error": f"pass_raised: {type(exc).__name__}: {exc}",
        }


async def _loop(interval: float) -> None:
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            poll, reason = should_poll_now()
            if poll:
                summary = _run_poll_pass()
                if summary["enabled"]:
                    _LOG.info(
                        "calendar_poll tick: fetched=%d ingested=%d "
                        "scheduled=%d completed=%d already_seen=%d "
                        "samus_owned=%d connect_error=%r",
                        summary["fetched"], summary["ingested"],
                        summary["scheduled"], summary["completed"],
                        summary["already_seen"], summary["samus_owned"],
                        summary["connect_error"],
                    )
                else:
                    if not getattr(_loop, "_warned_disabled", False):
                        _LOG.info(
                            "calendar_poll tick: disabled (oauth secrets "
                            "unset) — suppressing repeat",
                        )
                        _loop._warned_disabled = True  # type: ignore[attr-defined]
            else:
                _LOG.info("calendar_poll skip: %s", reason)
        except Exception:  # noqa: BLE001
            _LOG.exception("calendar_poll_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_calendar_poll_loop(app: Any) -> Optional[asyncio.Task]:
    if not _flag_on(ENV_ENABLED):
        _LOG.info("calendar_poll loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "calendar_poll_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(_loop(interval), name="samus.calendar_poll_loop")
    app.state.calendar_poll_task = task
    _LOG.info("calendar_poll loop started (interval=%.0fs)", interval)
    return task


async def stop_calendar_poll_loop(app: Any) -> None:
    task = getattr(app.state, "calendar_poll_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    app.state.calendar_poll_task = None
    _LOG.info("calendar_poll loop stopped")


__all__ = [
    "start_calendar_poll_loop",
    "stop_calendar_poll_loop",
    "should_poll_now",
    "ENV_ENABLED",
    "ENV_INTERVAL",
]
