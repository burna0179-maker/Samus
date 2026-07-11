"""Intake-internal Gmail inbox poller -- Samus drains its own inbox.

Design rationale (operator directive 2026-07-06):
  A Windows scheduled task (``Samus Inbox Poll``) was invoking
  ``scripts/Poll-Inbox.ps1`` every ~10 min to run one drain pass of the
  Gmail company inbox. That is external orchestration for behaviour that
  already belongs INSIDE the always-on ``samus-intake`` workcell -- the
  DPAPI secrets are (now) injected into the container, the CRM tables the
  poller writes to are already reachable, and the drain function itself is
  idempotent + fail-soft. The stack has ONE boot-time scheduled task
  (``Launch_svc_Samus``); every recurring behaviour lives inside the
  always-on container where the state is.

Same shape as :mod:`backend.gateway.control_tick_task` and
:mod:`backend.gateway.morning_ritual_task`: an asyncio loop started in
the intake FastAPI lifespan, running :func:`backend.intake.gmail_poller.drain_once`
on the interval. The drain function never raises -- a config-unset
container returns ``DrainPassResult(enabled=False)`` and the loop simply
records that fact; a Gmail auth/network fault sets ``connect_error`` and
the loop keeps ticking so the next pass has a chance.

Reasoning (the "when"): every ``ENV_INTERVAL_SEC`` (default 600s = 10 min,
matching the Windows-task cadence) the loop calls ``drain_once``. There
is no time-of-day window -- customer email arrives around the clock and
opt-out replies need to land on the suppression list within a pass or
two, not "in tomorrow's window". Reasoning is intentionally minimal
(master switch + settings presence via drain_once itself). Richer
signals (backoff on repeated connect_error, per-hour cadence variation)
can slot into ``should_poll_now`` without disturbing the scheduling seam.

Kill switches (composable, master defaults ON):
  * ``SAMUS_GMAIL_POLL_ENABLED``       - master arm, default ON
  * ``SAMUS_GMAIL_POLL_INTERVAL_SEC``  - poll cadence, default 600s
  * (Belt-and-suspenders) Disable-ScheduledTask -TaskName 'Samus Inbox Poll'
    on the host while the container-internal loop is armed. Both firing is
    safe -- the JSONL ledger + Gmail UNREAD-label removal dedupe messages
    across any number of concurrent drains, so a race just wastes a
    Gmail-API round-trip, never double-processes.

The Poll-Inbox.ps1 host script stays checked in through the transition
window as an explicit manual trigger; once the container loop has proven
itself for a week the script + the disabled Windows task can be removed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.intake.gmail_poll_task")

_DEFAULT_INTERVAL_SEC = 600.0
_INITIAL_DELAY_SEC = 60.0  # let boot churn settle before the first drain

ENV_ENABLED = "SAMUS_GMAIL_POLL_ENABLED"
ENV_INTERVAL = "SAMUS_GMAIL_POLL_INTERVAL_SEC"


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
    """Reasoning core: should the poller drain on this tick?

    Returns ``(decision, reason)`` -- a decision plus a one-line narrative
    so logs are easy to audit. Kept pure so the master-switch behaviour is
    trivially testable; the settings-present check is left to ``drain_once``
    itself (it already returns ``enabled=False`` when the OAuth trio is
    unset, so a redundant check here would drift from the drain path).
    """
    if not _flag_on(ENV_ENABLED):
        return False, "master disabled"
    return True, "master enabled"


def _run_drain_pass() -> dict[str, Any]:
    """One drain pass. Never raises -- ``drain_once`` is already fail-soft,
    but we still wrap in case a downstream import raises at first use."""
    try:
        from backend.intake.gmail_poller import drain_once

        result = drain_once()
        return {
            "enabled": bool(result.enabled),
            "fetched": int(result.fetched),
            "processed": int(result.processed),
            "duplicates": int(result.duplicates),
            "failed": int(result.failed),
            "connect_error": result.connect_error or "",
        }
    except Exception as exc:  # noqa: BLE001 -- loop must never die
        _LOG.exception("gmail_poll drain raised (fail-soft): %s", exc)
        return {
            "enabled": False,
            "fetched": 0,
            "processed": 0,
            "duplicates": 0,
            "failed": 0,
            "connect_error": f"drain_raised: {type(exc).__name__}: {exc}",
        }


async def _gmail_poll_loop(interval: float) -> None:
    """Poll every ``interval`` seconds; drain the inbox when reasoning says yes.

    Structure mirrors ``control_tick_task._control_tick_loop`` so an operator
    reading either module sees one shape. A tick fault is logged and the
    loop keeps going -- an inbox that cannot be reached this pass will be
    reached (or fail with the same signature) on the next.
    """
    try:
        await asyncio.sleep(_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            poll, reason = should_poll_now()
            if poll:
                summary = _run_drain_pass()
                if summary["enabled"]:
                    _LOG.info(
                        "gmail_poll tick: fetched=%d processed=%d "
                        "duplicates=%d failed=%d connect_error=%r",
                        summary["fetched"],
                        summary["processed"],
                        summary["duplicates"],
                        summary["failed"],
                        summary["connect_error"],
                    )
                else:
                    # Log the "config missing" state at INFO once per hour
                    # boundary so operators see the poller is alive without
                    # spamming every 10 minutes when Gmail secrets are unset.
                    if not getattr(_gmail_poll_loop, "_warned_disabled", False):
                        _LOG.info(
                            "gmail_poll tick: disabled by drain_once "
                            "(SAMUS_GMAIL_INBOX_EMAIL / OAUTH_CLIENT_ID / "
                            "OAUTH_CLIENT_SECRET unset) -- suppressing repeat"
                        )
                        _gmail_poll_loop._warned_disabled = True  # type: ignore[attr-defined]
            else:
                _LOG.info("gmail_poll skip: %s", reason)
        except Exception:  # noqa: BLE001 -- a tick fault never kills the loop
            _LOG.exception("gmail_poll_loop tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_gmail_poll_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container Gmail poll loop. Idempotent. Default ON."""
    if not _flag_on(ENV_ENABLED):
        _LOG.info("gmail_poll loop disabled (%s)", ENV_ENABLED)
        return None
    existing = getattr(app.state, "gmail_poll_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _float_env(ENV_INTERVAL, _DEFAULT_INTERVAL_SEC)
    task = asyncio.create_task(
        _gmail_poll_loop(interval),
        name="samus.gmail_poll_loop",
    )
    app.state.gmail_poll_task = task
    _LOG.info("gmail_poll loop started (interval=%.0fs)", interval)
    return task


async def stop_gmail_poll_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "gmail_poll_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 -- teardown swallows
        pass
    app.state.gmail_poll_task = None
    _LOG.info("gmail_poll loop stopped")


__all__ = [
    "start_gmail_poll_loop",
    "stop_gmail_poll_loop",
    "should_poll_now",
    "ENV_ENABLED",
    "ENV_INTERVAL",
]
