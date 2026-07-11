"""Idle-production drive — the self-pacing engine (ADR-016 phase 2).

Replaces the external Windows-task prompter with the agent's own reasoning. On
each control tick Samus OBSERVES whether production has gone idle during business
hours and DECIDES — from goal pace, not a cron — whether to progress it. When it
decides to produce, it actuates ONLY through the governed channels (the governed
dial policy for calls; the stake-gated outreach batch for email), so every
autonomous action still clears the Codex.

Two layers, kept apart so the judgment is pure and testable:

  - :func:`decide_idle_production` — PURE. Given observations (enabled, now, last
    activity, business-hours, pace) it returns an :class:`IdleDriveDecision`. No
    I/O, no clock, no settings read.
  - :func:`run_idle_drive` — the control-tick entrypoint. Reads the arm flag,
    gathers observations via an injected observer, decides, and on
    ``should_produce`` hands a bounded plan to an injected producer. Never
    raises; disarmed => observe-only no-op.

Wire-not-arm: gated by ``SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED`` (default OFF). When
off the drive still observes and reports, but the producer is never invoked, so
no autonomous production can occur.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.cash_engine.idle_production")

# How long production must be quiet during business hours before the drive will
# act. Coarse on purpose: the per-prospect fences (business-hours, cooldown,
# daily cap, DNC) are re-checked at actuation time inside the governed channel.
_IDLE_THRESHOLD_S = 45.0 * 60.0

# The agent's own operating window — the coarse gate on WHEN it may self-initiate
# production at all. America/Los_Angeles (HustleForge HQ), weekdays, 08:00–18:00.
# This is deliberately tighter than the per-prospect TCPA call window (8–21 local),
# which is still enforced independently at dial time inside the governed channel.
_DRIVE_WINDOW_HOURS = (8, 18)
_ACTIVITY_LOOKBACK_DAYS = 2


@dataclass
class IdleDriveDecision:
    should_produce: bool
    reason: str
    idle_seconds: float
    enabled: bool
    in_business_hours: bool
    behind_pace: Optional[bool] = None  # echoed so the producer can size intensity


@dataclass
class IdleObservation:
    """What the drive sees this tick. ``last_activity_ts`` is epoch seconds of the
    most recent production action (call placed or email sent), or ``None`` if no
    activity has been observed. ``behind_pace`` is a soft goal signal: ``True`` =
    behind and should push, ``False`` = on/ahead of pace so hold, ``None`` =
    unknown (treat idleness alone as reason to produce)."""
    last_activity_ts: Optional[float]
    in_business_hours: bool
    behind_pace: Optional[bool] = None


def decide_idle_production(
    *,
    enabled: bool,
    now_ts: float,
    last_activity_ts: Optional[float],
    in_business_hours: bool,
    behind_pace: Optional[bool] = None,
    idle_threshold_s: float = _IDLE_THRESHOLD_S,
) -> IdleDriveDecision:
    """Decide whether to progress production this tick. Pure — no side effects.

    Order of gates (each fail-closed toward NOT producing):
      1. disarmed          -> never produce
      2. outside business  -> never produce (no off-hours actuation, ever)
      3. recently active   -> hold; production is already flowing
      4. on/ahead of pace  -> hold; don't burn resources when the goal is met
      5. idle + in-hours (+ behind or pace unknown) -> PRODUCE
    A ``None`` ``last_activity_ts`` is treated as fully idle (cold start): if the
    other gates pass, the drive produces to prime the pipeline.
    """
    if not enabled:
        return IdleDriveDecision(False, "disarmed", 0.0, enabled,
                                 in_business_hours, behind_pace=behind_pace)
    if not in_business_hours:
        return IdleDriveDecision(False, "off-hours", 0.0, enabled,
                                 in_business_hours, behind_pace=behind_pace)

    if last_activity_ts is None:
        idle_seconds = idle_threshold_s  # cold: as idle as the threshold
    else:
        idle_seconds = max(0.0, now_ts - float(last_activity_ts))

    if idle_seconds < idle_threshold_s:
        return IdleDriveDecision(
            False, f"recently active ({idle_seconds / 60.0:.0f}m ago)",
            idle_seconds, enabled, in_business_hours, behind_pace=behind_pace,
        )
    if behind_pace is False:
        return IdleDriveDecision(
            False, "on/ahead of pace", idle_seconds, enabled,
            in_business_hours, behind_pace=behind_pace,
        )

    pace_note = "behind pace" if behind_pace else "pace unknown"
    return IdleDriveDecision(
        True, f"idle {idle_seconds / 60.0:.0f}m during business hours; {pace_note}",
        idle_seconds, enabled, in_business_hours, behind_pace=behind_pace,
    )


def _parse_iso_epoch(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp to epoch seconds; None if unparseable. Handles
    a trailing 'Z' and naive strings (assumed UTC)."""
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def _recent_days(now_utc: datetime, lookback: int = _ACTIVITY_LOOKBACK_DAYS) -> list[str]:
    """YYYYMMDD strings for today..today-lookback in the drive timezone.

    Compact form — matches the dial-run ledger filenames
    (``dial_run_<YYYYMMDD>T...json``)."""
    from datetime import timedelta

    day = now_utc.date()
    return [(day - timedelta(days=n)).strftime("%Y%m%d") for n in range(lookback + 1)]


def _recent_days_iso(now_utc: datetime, lookback: int = _ACTIVITY_LOOKBACK_DAYS) -> list[str]:
    """YYYY-MM-DD (ISO, dashed) strings for today..today-lookback.

    The outreach batch + voicemail-draft artifacts are named with the ISO
    date (``morning_batch_2026-07-03.jsonl``, ``<pid>_2026-07-03.txt``) — a
    DIFFERENT convention from the dial-run ledgers. Reading them with the
    compact ``_recent_days`` form silently matched nothing, so email + draft
    production was invisible to the idle signal and the drive believed it had
    been idle for ~a day even mid-production (found via the production pulse
    reporting 'idle 1329m' while sends flowed, 2026-07-03)."""
    from datetime import timedelta

    day = now_utc.date()
    return [(day - timedelta(days=n)).isoformat() for n in range(lookback + 1)]


def _last_call_activity_ts(now_utc: Optional[datetime] = None) -> Optional[float]:
    """Epoch of the most recent PLACED call from the dial-run ledgers, or None.

    Reads ``<root>/voice/dial_runs/dial_run_<YYYYMMDD>*.json`` — each has an
    ``attempts`` list; an attempt with ``outcome == 'initiated'`` and an
    ``initiated_at`` is a real placed call. Fully defensive."""
    from backend.common import storage

    now = now_utc or datetime.now(timezone.utc)
    latest: Optional[float] = None
    try:
        runs_dir = storage.root() / "voice" / "dial_runs"
    except Exception:  # noqa: BLE001
        return None
    for day_str in _recent_days(now):
        try:
            files = list(runs_dir.glob(f"dial_run_{day_str}*.json"))
        except Exception:  # noqa: BLE001
            continue
        for fpath in files:
            try:
                import json

                data = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for attempt in data.get("attempts", []):
                if (attempt.get("outcome") or "").lower() != "initiated":
                    continue
                ts = _parse_iso_epoch(attempt.get("initiated_at"))
                if ts is not None and (latest is None or ts > latest):
                    latest = ts
    return latest


def _last_email_activity_ts(now_utc: Optional[datetime] = None) -> Optional[float]:
    """Epoch of the most recent SENT outreach email from the batch ledgers, or None.

    Reads ``morning_batch_<YYYYMMDD>.jsonl`` (rows with ``status == 'sent'``),
    checking both the root and an ``outreach/`` subdir. Fully defensive."""
    from backend.common import storage

    now = now_utc or datetime.now(timezone.utc)
    latest: Optional[float] = None
    try:
        root = storage.root()
    except Exception:  # noqa: BLE001
        return None
    for day_str in _recent_days_iso(now):  # ISO date — matches the writer
        for base in (root, root / "outreach"):
            fpath = base / f"morning_batch_{day_str}.jsonl"
            try:
                if not fpath.is_file():
                    continue
                lines = fpath.read_text(encoding="utf-8").splitlines()
            except Exception:  # noqa: BLE001
                continue
            for line in lines:
                try:
                    import json

                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if (row.get("status") or "").lower() != "sent":
                    continue
                ts = _parse_iso_epoch(row.get("ts"))
                if ts is not None and (latest is None or ts > latest):
                    latest = ts
    return latest


def _in_drive_business_hours(now_utc: Optional[datetime] = None) -> bool:
    """True iff it is a weekday inside the agent's operating window (Pacific)."""
    try:
        from backend.common.us_timezones import state_to_timezone

        tz = state_to_timezone("CA")  # America/Los_Angeles
    except Exception:  # noqa: BLE001
        return False
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    if now_local.weekday() >= 5:  # Sat/Sun — no weekend self-initiation
        return False
    return _DRIVE_WINDOW_HOURS[0] <= now_local.hour < _DRIVE_WINDOW_HOURS[1]


def _last_voicemail_draft_activity_ts(now_utc: Optional[datetime] = None) -> Optional[float]:
    """Epoch (file mtime) of the most recent voicemail DRAFT written, or None.

    A cold voice pass produces drafts (ADR-002), never placed calls, so
    without this a full day of governed voice production reads as zero voice
    activity and the idle signal never registers it. Drafts land at
    ``<root>/voice/voicemail_drafts/<pid>_<YYYY-MM-DD>.txt``; we take the
    newest file's mtime across the recent ISO days. Fully defensive."""
    from backend.common import storage

    now = now_utc or datetime.now(timezone.utc)
    latest: Optional[float] = None
    try:
        d = storage.root() / "voice" / "voicemail_drafts"
    except Exception:  # noqa: BLE001
        return None
    for day_str in _recent_days_iso(now):
        try:
            files = list(d.glob(f"*_{day_str}.txt"))
        except Exception:  # noqa: BLE001
            continue
        for fpath in files:
            try:
                mtime = fpath.stat().st_mtime
            except Exception:  # noqa: BLE001
                continue
            if latest is None or mtime > latest:
                latest = mtime
    return latest


def _default_observer() -> IdleObservation:
    """Real observation of production state. Defensive at every read: a failure
    degrades to the safest reading (no recent activity known, hold on hours) so
    the drive never actuates on a blind or partial view."""
    now = datetime.now(timezone.utc)
    call_ts = _last_call_activity_ts(now)
    email_ts = _last_email_activity_ts(now)
    # Voicemail drafts are internal artifacts (operator review queue), not
    # customer-facing actions.  Counting them as activity suppresses the
    # idle drive indefinitely because the voice campaign writes drafts on
    # every control tick.  Only actual sends and placed calls should keep
    # the idle timer satisfied.
    candidates = [t for t in (call_ts, email_ts) if t is not None]
    last_activity = max(candidates) if candidates else None
    # behind_pace = are we covering our burn? MRR < CODB => behind => push.
    # None (unknown data) => idleness alone drives production at moderate intensity.
    try:
        from backend.cash_engine.goal_pace import default_behind_pace
        behind_pace = default_behind_pace()
    except Exception:  # noqa: BLE001 — a pace-read fault must never break observing
        behind_pace = None
    return IdleObservation(
        last_activity_ts=last_activity,
        in_business_hours=_in_drive_business_hours(now),
        behind_pace=behind_pace,
    )


def _default_producer(decision: IdleDriveDecision) -> dict[str, Any]:
    """Actuate production via the campaign portfolio: the agent selects as many
    governed campaigns as goal pace calls for, bounded by monitoring capacity,
    and runs each for one paced pass. Seeded campaigns are the stake-gated email
    batch and the consent-routed voice pass (cold => voicemail draft per ADR-002;
    consented + fenced => governed live dial). See
    backend/cash_engine/campaign_portfolio.py. Fail-soft — a fault degrades to
    'nothing produced', never a raise.

    Intensity is min(revenue-urgency, affordability): ``behind_pace`` (CODB
    coverage + deadline run-rate) sets how hard the revenue gap wants to push;
    ``assess_affordability`` (the CODB reasoner's headroom + safety reserve) caps
    what we can responsibly spend, gating cost tiers + scaling volume by posture."""
    from backend.cash_engine.affordability import assess_affordability
    from backend.cash_engine.campaign_portfolio import (
        default_portfolio_deps,
        run_campaign_portfolio,
    )

    affordability = assess_affordability()
    return run_campaign_portfolio(
        # Pass affordability into the deps so the voice campaign gates its PAID
        # live-dial escalation on the CODB posture (invest => live calls;
        # conserve/lean => voicemail drafts) — the reasoner is the call-volume
        # ceiling, no separate cap.
        deps=default_portfolio_deps(affordability),
        behind_pace=getattr(decision, "behind_pace", None),
        affordability=affordability,
    )


def run_idle_drive(
    *,
    now_ts: Optional[float] = None,
    observer: Optional[Callable[[], IdleObservation]] = None,
    producer: Optional[Callable[[IdleDriveDecision], dict[str, Any]]] = None,
    idle_threshold_s: Optional[float] = None,
) -> dict[str, Any]:
    """Control-tick entrypoint. Observe -> decide -> (maybe) produce. Never raises.

    Returns a summary dict with ``ok`` always True (a drive fault must not fail
    the whole control tick), ``enabled``, ``produced``, ``reason``, and, when it
    acted, the producer ``actuation`` outcome.
    """
    from backend.common.settings import get_settings

    st = get_settings()
    enabled = bool(getattr(st, "idle_production_drive_enabled", False))
    now = time.time() if now_ts is None else float(now_ts)
    threshold = _IDLE_THRESHOLD_S if idle_threshold_s is None else float(idle_threshold_s)

    try:
        obs = (observer or _default_observer)()
    except Exception as exc:  # noqa: BLE001 — observation fault => hold, never crash
        _LOG.warning("idle-drive observer failed: %s", exc)
        return {"ok": True, "enabled": enabled, "produced": False,
                "reason": f"observer-error: {exc}"}

    decision = decide_idle_production(
        enabled=enabled, now_ts=now, last_activity_ts=obs.last_activity_ts,
        in_business_hours=obs.in_business_hours, behind_pace=obs.behind_pace,
        idle_threshold_s=threshold,
    )
    summary: dict[str, Any] = {
        "ok": True, "enabled": enabled, "produced": False,
        "reason": decision.reason, "idle_seconds": round(decision.idle_seconds, 1),
    }
    if not decision.should_produce:
        return summary

    try:
        actuation = (producer or _default_producer)(decision)
        summary["actuation"] = actuation
        # HONEST telemetry: "produced" means real campaigns initiated, not
        # merely "the producer ran". Before 2026-07-03 this was
        # unconditionally True, so a fully starved portfolio (initiated=0,
        # every campaign skipped ineligible) read as healthy production on
        # every tick while the idle counter kept climbing.
        if isinstance(actuation, dict) and "initiated" in actuation:
            summary["produced"] = int(actuation.get("initiated") or 0) > 0
        else:
            summary["produced"] = True
    except Exception as exc:  # noqa: BLE001 — a producer fault is a hold, not a crash
        _LOG.warning("idle-drive producer failed: %s", exc)
        summary["reason"] = f"producer-error: {exc}"
        return summary

    # Starvation awareness + self-supply (backend/cash_engine/self_supply.py):
    # diagnose why an actuation yielded nothing and — when armed — restock the
    # missing input through the owning workcell instead of waiting for a host
    # cron that may never fire. Fail-soft: a self-supply fault never breaks
    # the tick.
    try:
        from backend.cash_engine.self_supply import run_self_supply

        supply = run_self_supply(summary.get("actuation"))
        if supply.get("starved"):
            summary["self_supply"] = supply
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("self-supply hook failed: %s", exc)
    return summary
