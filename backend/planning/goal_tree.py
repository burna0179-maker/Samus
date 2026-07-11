"""Goal-tree seeding + decomposition arithmetic.

Turns the company's real revenue target (the constants ``_REVENUE_TARGET_USD``
/ ``_REVENUE_TARGET_DATE`` in ``backend/cognitive/intelligence_cycle.py``) into
a persisted, decomposed goal tree:

    year  -> 90d -> 30d -> weekly (campaign revenue) -> daily (lead/task counts)

Decomposition arithmetic (framework Phase 5):

    needed_leads = target_revenue / (avg_deal_usd * close_rate)

* ``avg_deal_usd`` and ``close_rate`` are read from the closed-loop economics
  when available (ROI roll-up conversion history + CRM tier close
  probabilities), degrading to conservative defaults so seeding always
  produces a tree even on a cold system.
* Revenue splits proportionally by days in each window (a 30d window carries
  30/period_days of the parent's revenue); lead/task counts derive from the
  weekly revenue via the funnel arithmetic above.

Idempotent: re-seeding refreshes the SAME deterministic goal ids (namespaced by
horizon + period) in place rather than spawning duplicates.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from . import store
from .models import (
    GOAL_ACTIVE,
    HORIZON_30D,
    HORIZON_90D,
    HORIZON_DAY,
    HORIZON_WEEK,
    HORIZON_YEAR,
    Goal,
)

_LOG = logging.getLogger("samus.planning.goal_tree")

# Conservative economics defaults when the closed-loop data is thin. A HF
# engagement averages ~$3.5k annualised (scoring._BUDGET_MIDPOINTS_USD mid) and
# a warm-lead close rate is ~15% (scoring.tier_close_probability("warm")).
_DEFAULT_AVG_DEAL_USD = 3500.0
_DEFAULT_CLOSE_RATE = 0.15
# Floor so a degenerate close_rate/avg_deal never divides to infinity.
_MIN_CLOSE_RATE = 0.02
_MIN_AVG_DEAL_USD = 100.0

# Roughly one qualified task per lead touched (call or email follow-up).
_TASKS_PER_LEAD = 1.0


@dataclass
class FunnelEconomics:
    """The close-loop rates the decomposition divides by."""

    avg_deal_usd: float
    close_rate: float
    source: str

    def leads_for_revenue(self, revenue_usd: float) -> float:
        denom = max(_MIN_AVG_DEAL_USD, self.avg_deal_usd) * max(
            _MIN_CLOSE_RATE, self.close_rate
        )
        if denom <= 0:
            return 0.0
        return max(0.0, float(revenue_usd)) / denom


def _revenue_target() -> tuple[float, _dt.date]:
    """The real revenue target + deadline (from the cognitive constants)."""
    try:
        from backend.cognitive.intelligence_cycle import (
            _REVENUE_TARGET_DATE,
            _REVENUE_TARGET_USD,
        )

        return float(_REVENUE_TARGET_USD), _REVENUE_TARGET_DATE
    except Exception as exc:  # noqa: BLE001 — degrade to a safe default target
        _LOG.warning("goal_tree: revenue target constants unreadable: %s", exc)
        return 40000.0, _dt.date.today() + _dt.timedelta(days=90)


def read_funnel_economics() -> FunnelEconomics:
    """Derive (avg_deal_usd, close_rate) from closed-loop data, else defaults.

    Best-effort: reads recent ROI roll-ups for realised average deal size and
    the CRM scoring tier close-probabilities for the close rate. Any gap
    degrades that component to its conservative default — the function never
    raises and always returns a usable economics object.
    """
    avg_deal = _DEFAULT_AVG_DEAL_USD
    close_rate = _DEFAULT_CLOSE_RATE
    source_bits: list[str] = []

    # -- realised average deal size from recent ROI roll-ups ------------------
    # Today's roll-up is recomputed live; prior days are served from the store
    # (a missing day simply contributes nothing).
    try:
        from backend.finance.roi import get_rollup, load_rollup

        today = _dt.datetime.now(_dt.timezone.utc).date()
        rev_total = 0.0
        conv_total = 0
        for back in range(0, 30):
            day = (today - _dt.timedelta(days=back)).isoformat()
            roll = get_rollup(day) if back == 0 else load_rollup(day)
            if not isinstance(roll, dict):
                continue
            rev_total += float(roll.get("revenue_usd") or 0.0)
            conv_total += int(roll.get("conversion_count") or 0)
        if conv_total > 0 and rev_total > 0:
            avg_deal = rev_total / conv_total
            source_bits.append("roi_rollup_avg_deal")
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("goal_tree: roi avg-deal read degraded: %s", exc)

    # -- close rate from CRM tier probabilities (warm baseline) ---------------
    try:
        from backend.crm.scoring import tier_close_probability

        close_rate = float(tier_close_probability("warm"))
        source_bits.append("crm_tier_close_rate")
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("goal_tree: close-rate read degraded: %s", exc)

    return FunnelEconomics(
        avg_deal_usd=round(avg_deal, 2),
        close_rate=round(close_rate, 4),
        source=("+".join(source_bits) or "defaults"),
    )


def _goal_id(horizon: str, period_key: str) -> str:
    """Deterministic id so re-seeding refreshes in place (idempotent)."""
    return f"goal::{horizon}::{period_key}"


def _iso(d: _dt.date) -> str:
    return d.isoformat()


def build_goal_tree(
    *,
    target_usd: float | None = None,
    target_date: _dt.date | None = None,
    today: _dt.date | None = None,
    economics: FunnelEconomics | None = None,
) -> list[Goal]:
    """Build (but do not persist) the decomposed goal tree. Pure given inputs.

    Returns goals in tree order: [year, 90d, 30d, week, day]. The daily goals
    are lead/task COUNT goals derived from the weekly revenue via the funnel;
    every other horizon is a revenue goal.
    """
    if target_usd is None or target_date is None:
        tgt_usd, tgt_date = _revenue_target()
        target_usd = tgt_usd if target_usd is None else target_usd
        target_date = tgt_date if target_date is None else target_date
    today = today or _dt.date.today()
    econ = economics or read_funnel_economics()

    # Days remaining to the deadline (min 1 so we never divide by zero, and so
    # a past-deadline target still produces a "catch up now" daily goal).
    days_remaining = max(1, (target_date - today).days)

    goals: list[Goal] = []

    # -- year (root) ----------------------------------------------------------
    year_key = str(target_date.year)
    year_goal = Goal(
        id=_goal_id(HORIZON_YEAR, year_key),
        horizon=HORIZON_YEAR,
        target_metric="revenue_usd",
        target_value=round(float(target_usd), 2),
        parent_id="",
        status=GOAL_ACTIVE,
        label=f"Reach ${float(target_usd):,.0f} revenue by {target_date.isoformat()}",
        period_start=_iso(today),
        period_end=_iso(target_date),
        metadata={
            "target_date": target_date.isoformat(),
            "days_remaining": days_remaining,
            "avg_deal_usd": econ.avg_deal_usd,
            "close_rate": econ.close_rate,
            "economics_source": econ.source,
        },
    )
    goals.append(year_goal)

    # Daily revenue run-rate needed to hit the target by the deadline.
    daily_revenue = float(target_usd) / days_remaining

    # -- 90d ------------------------------------------------------------------
    win_90 = min(90, days_remaining)
    end_90 = today + _dt.timedelta(days=win_90)
    rev_90 = round(daily_revenue * win_90, 2)
    goal_90 = Goal(
        id=_goal_id(HORIZON_90D, _iso(today)),
        horizon=HORIZON_90D,
        target_metric="revenue_usd",
        target_value=rev_90,
        parent_id=year_goal.id,
        status=GOAL_ACTIVE,
        label=f"90-day revenue: ${rev_90:,.0f}",
        period_start=_iso(today),
        period_end=_iso(end_90),
        metadata={"window_days": win_90},
    )
    goals.append(goal_90)

    # -- 30d ------------------------------------------------------------------
    win_30 = min(30, days_remaining)
    end_30 = today + _dt.timedelta(days=win_30)
    rev_30 = round(daily_revenue * win_30, 2)
    goal_30 = Goal(
        id=_goal_id(HORIZON_30D, _iso(today)),
        horizon=HORIZON_30D,
        target_metric="revenue_usd",
        target_value=rev_30,
        parent_id=goal_90.id,
        status=GOAL_ACTIVE,
        label=f"30-day revenue: ${rev_30:,.0f}",
        period_start=_iso(today),
        period_end=_iso(end_30),
        metadata={"window_days": win_30},
    )
    goals.append(goal_30)

    # -- weekly (campaign revenue) --------------------------------------------
    win_7 = min(7, days_remaining)
    end_7 = today + _dt.timedelta(days=win_7)
    rev_7 = round(daily_revenue * win_7, 2)
    goal_week = Goal(
        id=_goal_id(HORIZON_WEEK, _iso(today)),
        horizon=HORIZON_WEEK,
        target_metric="revenue_usd",
        target_value=rev_7,
        parent_id=goal_30.id,
        status=GOAL_ACTIVE,
        label=f"This week's campaign revenue: ${rev_7:,.0f}",
        period_start=_iso(today),
        period_end=_iso(end_7),
        metadata={"window_days": win_7},
    )
    goals.append(goal_week)

    # -- daily (lead + task COUNT goals from the weekly revenue) --------------
    weekly_leads = econ.leads_for_revenue(rev_7)
    daily_leads = weekly_leads / max(1, win_7)
    # Round up — you need at least this many leads/day to stay on the run-rate.
    daily_leads_int = max(1, int(daily_leads + 0.999))
    daily_tasks_int = max(1, int(daily_leads_int * _TASKS_PER_LEAD + 0.999))

    goal_day_leads = Goal(
        id=_goal_id(HORIZON_DAY, f"{_iso(today)}::leads"),
        horizon=HORIZON_DAY,
        target_metric="leads_created",
        target_value=float(daily_leads_int),
        parent_id=goal_week.id,
        status=GOAL_ACTIVE,
        label=f"Create {daily_leads_int} leads/day",
        period_start=_iso(today),
        period_end=_iso(today + _dt.timedelta(days=1)),
        metadata={
            "derived_from_weekly_revenue_usd": rev_7,
            "avg_deal_usd": econ.avg_deal_usd,
            "close_rate": econ.close_rate,
            "weekly_leads_needed": round(weekly_leads, 2),
        },
    )
    goals.append(goal_day_leads)

    goal_day_tasks = Goal(
        id=_goal_id(HORIZON_DAY, f"{_iso(today)}::tasks"),
        horizon=HORIZON_DAY,
        target_metric="tasks_completed",
        target_value=float(daily_tasks_int),
        parent_id=goal_week.id,
        status=GOAL_ACTIVE,
        label=f"Complete {daily_tasks_int} outreach tasks/day",
        period_start=_iso(today),
        period_end=_iso(today + _dt.timedelta(days=1)),
        metadata={"tasks_per_lead": _TASKS_PER_LEAD},
    )
    goals.append(goal_day_tasks)

    return goals


def seed_goal_tree(
    *,
    target_usd: float | None = None,
    target_date: _dt.date | None = None,
    today: _dt.date | None = None,
) -> list[Goal]:
    """Build + persist the goal tree (idempotent by deterministic ids).

    Returns the persisted goals. Never raises — a persistence failure logs and
    still returns the in-memory tree so a caller can inspect it.
    """
    goals = build_goal_tree(
        target_usd=target_usd, target_date=target_date, today=today,
    )
    try:
        store.save_goals(goals)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("seed_goal_tree persist failed: %s", exc)
    return goals


def ensure_goal_tree(today: _dt.date | None = None) -> list[Goal]:
    """Seed the tree once if no active goals exist; else return the current tree.

    The idempotent entry point the planner calls on each control tick — cheap
    when the tree already exists (a single list_goals read), self-healing when
    it does not.
    """
    existing = store.list_goals(status=GOAL_ACTIVE)
    if existing:
        return existing
    return seed_goal_tree(today=today)


__all__ = [
    "FunnelEconomics",
    "read_funnel_economics",
    "build_goal_tree",
    "seed_goal_tree",
    "ensure_goal_tree",
]
