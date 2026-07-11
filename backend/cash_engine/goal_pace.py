"""Goal-pace signal — is Samus behind on its financial goal right now?

Feeds ``behind_pace`` into the idle-production drive + campaign portfolio: True =>
push production to capacity, False => on/ahead so hold, None => unknown (moderate
default). The honest, load-bearing signal is COVERAGE OF BURN:

    behind_pace = current_MRR < CODB_monthly_burn

CODB (cost of doing business) is the break-even BAR — the recurring monthly burn
the business must cover to be sustainable. It is NOT a pace by itself; pace is
revenue measured against that bar. While MRR sits below CODB the business is
losing money every month, so the agent is behind and should produce as much as
its monitoring capacity allows. As MRR approaches and clears CODB the signal
flips and the drive eases off.

Both sides are injected readers so the logic is pure + testable and the MRR
source can be upgraded (a precise, throttled Stripe total) without touching the
decision. Every default reader is defensive: on any doubt it returns None, and
:func:`compute_behind_pace` maps a missing side to None (unknown => moderate),
never to a false "behind" that would over-produce.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

_LOG = logging.getLogger("samus.cash_engine.goal_pace")


def _coverage_signal(
    *,
    mrr_reader: Callable[[], Optional[float]],
    codb_reader: Callable[[], Optional[float]],
) -> Optional[bool]:
    """Burn-coverage: True if MRR is below the CODB break-even bar (behind);
    False if it covers burn; None if either side is unknown. Pure; reader faults
    swallowed to None."""
    try:
        codb = codb_reader()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("codb read failed: %s", exc)
        codb = None
    try:
        mrr = mrr_reader()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("mrr read failed: %s", exc)
        mrr = None

    if codb is None or mrr is None:
        return None
    if codb <= 0:
        # No burn on record — cannot judge coverage; stay moderate rather than
        # declare us "ahead" (which would silence the drive).
        return None
    return float(mrr) < float(codb)


def deadline_behind(
    *,
    goal_usd: float,
    total_campaign_days: int,
    days_remaining: int,
    recent_daily_revenue: Optional[float],
) -> Optional[bool]:
    """Velocity-based deadline pace: is the recent revenue RUN-RATE enough to hit
    the goal by the deadline? behind if ``recent_daily_revenue`` is below the
    required daily rate (``goal / total_campaign_days``). No cumulative ledger
    needed — only current velocity vs required velocity. None when velocity is
    unknown or the deadline has passed (defer to the coverage signal)."""
    if recent_daily_revenue is None or goal_usd <= 0 or total_campaign_days <= 0:
        return None
    if days_remaining <= 0:
        return None  # deadline moot -> let coverage decide
    required_daily = goal_usd / float(total_campaign_days)
    return float(recent_daily_revenue) < required_daily


def _combine(*signals: Optional[bool]) -> Optional[bool]:
    """OR over the present signals: behind if ANY says behind; False if all
    present say not-behind; None if none are present (moderate default)."""
    present = [s for s in signals if s is not None]
    if not present:
        return None
    return any(present)


def compute_behind_pace(
    *,
    mrr_reader: Callable[[], Optional[float]],
    codb_reader: Callable[[], Optional[float]],
    deadline_reader: Optional[Callable[[], Optional[bool]]] = None,
) -> Optional[bool]:
    """Overall pace: behind if we fail EITHER burn coverage OR the deadline
    run-rate. True => push, False => hold, None => moderate. Pure over its
    injected readers; every fault degrades to None (never a false 'behind')."""
    coverage = _coverage_signal(mrr_reader=mrr_reader, codb_reader=codb_reader)
    deadline: Optional[bool] = None
    if deadline_reader is not None:
        try:
            deadline = deadline_reader()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("deadline read failed: %s", exc)
            deadline = None
    return _combine(coverage, deadline)


# ---------------------------------------------------------------------------
# Default readers.
# ---------------------------------------------------------------------------

def _default_codb_reader() -> Optional[float]:
    """Total monthly burn from the CODB registry. Standalone (no Stripe)."""
    try:
        from backend.finance.service import get_codb_summary
        return float(get_codb_summary().total_monthly_burn_usd)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("codb summary unavailable: %s", exc)
        return None


def _default_mrr_reader() -> Optional[float]:
    """Best CHEAP MRR estimate without a live Stripe call: the sum of recurring
    subscription adds recorded in the webhook event log.

    NOTE — this is a disk LOWER BOUND, not authoritative total MRR: it only sees
    subscriptions that arrived through the webhook log, and only within the
    window. That is deliberately safe for the current regime (MRR is a small
    fraction of CODB, so any undercount still reads 'behind'), but it should be
    upgraded to a precise, throttled Stripe total before MRR approaches CODB,
    where accuracy near break-even starts to matter. Returns None (unknown, not
    zero) when the log is absent, so a missing ledger => moderate default rather
    than a false 'behind'."""
    try:
        from backend.finance import webhook as webhook_mod
        if not webhook_mod.event_log_path().exists():
            return None
        from backend.finance.service import get_mrr_adds
        # Wide window: capture all logged recurring adds as the best disk proxy.
        return float(get_mrr_adds(window_days=3650).total_mrr_usd)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("mrr estimate unavailable: %s", exc)
        return None


_VELOCITY_WINDOW_DAYS = 7


def _recent_daily_revenue(days: int = _VELOCITY_WINDOW_DAYS) -> Optional[float]:
    """Average daily collected revenue over the recent webhook window (sum of
    ``amount_total_usd`` / days). Disk-based, no live Stripe. None when the log
    is absent. Note: the event ledger has short retention, so this is a RECENT
    velocity, which is exactly what the deadline run-rate check wants."""
    try:
        from backend.finance import webhook as webhook_mod
        if not webhook_mod.event_log_path().exists():
            return None
        events = webhook_mod.load_recent_events(days)
        total = sum(float(getattr(e, "amount_total_usd", 0) or 0) for e in events)
        return total / float(max(1, days))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("revenue velocity unavailable: %s", exc)
        return None


def _default_deadline_signal() -> Optional[bool]:
    """Deadline run-rate signal from env-configured goal + recent velocity.
    SAMUS_GOAL_AMOUNT_USD (default 40000), SAMUS_GOAL_DEADLINE (YYYY-MM-DD,
    default 2026-07-12), SAMUS_GOAL_START (default 2026-06-12). None on any doubt."""
    import os
    from datetime import date

    try:
        goal = float(os.getenv("SAMUS_GOAL_AMOUNT_USD", "40000") or 0)
        deadline = date.fromisoformat(os.getenv("SAMUS_GOAL_DEADLINE", "2026-07-12"))
        start = date.fromisoformat(os.getenv("SAMUS_GOAL_START", "2026-06-12"))
        today = date.today()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("goal config unparseable: %s", exc)
        return None
    total_days = (deadline - start).days
    days_remaining = (deadline - today).days
    return deadline_behind(
        goal_usd=goal, total_campaign_days=total_days,
        days_remaining=days_remaining, recent_daily_revenue=_recent_daily_revenue(),
    )


def default_behind_pace() -> Optional[bool]:
    """Wire the default CODB + MRR + deadline readers. Used by the idle-drive
    observer. behind if we fail EITHER burn coverage OR the deadline run-rate."""
    return compute_behind_pace(
        mrr_reader=_default_mrr_reader,
        codb_reader=_default_codb_reader,
        deadline_reader=_default_deadline_signal,
    )


# ---------------------------------------------------------------------------
# Graded urgency score — the continuous sibling of behind_pace.
# ---------------------------------------------------------------------------

def _default_deadline_urgency() -> Optional[float]:
    """Deadline run-rate GAP as a 0..1 urgency: ``(required_daily - recent_daily)
    / required_daily`` clamped to [0, 1]. 0 => recent velocity meets/exceeds the
    required daily rate; 1 => zero recent revenue against a positive required
    rate. None on any doubt (unparseable config, no velocity, deadline passed) so
    a blind read never fabricates urgency. Same env config as
    :func:`_default_deadline_signal`."""
    import os
    from datetime import date

    try:
        goal = float(os.getenv("SAMUS_GOAL_AMOUNT_USD", "40000") or 0)
        deadline = date.fromisoformat(os.getenv("SAMUS_GOAL_DEADLINE", "2026-07-12"))
        start = date.fromisoformat(os.getenv("SAMUS_GOAL_START", "2026-06-12"))
        today = date.today()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("goal config unparseable: %s", exc)
        return None
    total_days = (deadline - start).days
    days_remaining = (deadline - today).days
    if goal <= 0 or total_days <= 0 or days_remaining <= 0:
        return None
    required_daily = goal / float(total_days)
    recent = _recent_daily_revenue()
    if recent is None or required_daily <= 0:
        return None
    return max(0.0, min(1.0, (required_daily - float(recent)) / required_daily))


def compute_urgency_score(
    *,
    mrr_reader: Callable[[], Optional[float]],
    codb_reader: Callable[[], Optional[float]],
    deadline_urgency_reader: Optional[Callable[[], Optional[float]]] = None,
) -> float:
    """Graded urgency in ``[0.0, 1.0]`` — the continuous sibling of
    :func:`compute_behind_pace`. 0.0 = on/ahead (ease off), 1.0 = maximally behind
    (push to capacity). It is the MAX of two gaps (mirroring behind_pace's OR):

      * burn-coverage gap     = ``(CODB - MRR) / CODB`` clamped [0, 1] — how far
        MRR sits below the break-even bar (1.0 at MRR=0).
      * deadline run-rate gap = ``(required_daily - recent_daily) / required_daily``.

    Pure over its injected readers. Every unknown contributes nothing (never a
    fabricated urgency); if EVERY signal is unknown it returns 0.5 (moderate —
    matching behind_pace's ``None`` default), so a blind financial read paces
    moderately rather than at either extreme."""
    coverage: Optional[float] = None
    try:
        codb = codb_reader()
        mrr = mrr_reader()
        if codb is not None and mrr is not None and float(codb) > 0:
            coverage = max(0.0, min(1.0, (float(codb) - float(mrr)) / float(codb)))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("coverage urgency read failed: %s", exc)

    deadline: Optional[float] = None
    if deadline_urgency_reader is not None:
        try:
            deadline = deadline_urgency_reader()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("deadline urgency read failed: %s", exc)

    present = [g for g in (coverage, deadline) if g is not None]
    if not present:
        return 0.5
    return max(present)


def default_urgency_score() -> float:
    """Wire the default CODB + MRR + deadline readers into a graded urgency.
    Used by the cold-dial cadence to size interval + volume to how far behind
    revenue is right now."""
    return compute_urgency_score(
        mrr_reader=_default_mrr_reader,
        codb_reader=_default_codb_reader,
        deadline_urgency_reader=_default_deadline_urgency,
    )
