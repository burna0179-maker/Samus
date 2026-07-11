"""Correlated health anchor - ties website design ambition to financial surplus.

The premise: the Hustleforge site only earns "huge tech firm" polish (3D renders,
animation, WebGL) when the business can actually afford to fund it. This governor
reads the REAL financial state (Samus finance: runway, balance, burn, MRR) and
turns *sustained* surplus into a design TIER + an earmarked design budget that
the house-growth loop and the website builder consult before reaching for
expensive visuals. Innovation is funded by surplus, not survival.

ANCHOR MECHANICS (why "correlated, with hysteresis")
  * Only liquid funds ABOVE a runway-safety floor are "designated to design"
    (a fraction of that surplus) - protecting the runway comes first.
  * The tier ratchets UP one step at a time and only after stability is
    SUSTAINED (a healthy streak), so a single good day never unlocks 3D.
  * The tier drops straight to conservative the moment runway falls below the
    floor or a runway alert fires - design ambition follows financial health
    DOWN fast and UP slow. That deliberate lag is the "anchor".

Best-effort + fail-safe: if the financial read is unavailable (no Stripe creds,
a dev shell, an outage) it yields the CONSERVATIVE tier - never an aspirational
one on missing data. Pure compute (``compute_design_budget``) is separated from
the I/O read (``read_financial_health``) so the policy is fully unit-testable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

_LOG = logging.getLogger("samus.growth.design_budget")

# Ordered tiers (index = rank). Each unlocks the capabilities of all lower tiers.
TIERS: tuple[str, ...] = ("conservative", "standard", "premium", "flagship")

_TIER_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "conservative": ("clean_static", "deterministic_templates"),
    "standard": ("css_transitions", "optimized_imagery", "basic_motion"),
    "premium": ("scroll_animations", "custom_illustration", "motion_design", "video_hero"),
    "flagship": ("threed_renders", "webgl_interactive", "hero_animation", "full_motion_design"),
}

# Design funds (USD earmarked) needed to be ELIGIBLE for each tier.
_TIER_FUNDS_FLOOR: dict[str, float] = {
    "conservative": 0.0,
    "standard": 200.0,
    "premium": 1000.0,
    "flagship": 5000.0,
}

# Consecutive healthy passes required before ratcheting UP into each tier.
_TIER_STREAK_REQUIRED: dict[str, int] = {
    "conservative": 0,
    "standard": 1,
    "premium": 2,
    "flagship": 4,
}

# Per-cycle design spend ceiling by tier (never exceeds the earmarked funds).
_TIER_SPEND_CAP: dict[str, float] = {
    "conservative": 0.0,    # no paid design spend - protect runway
    "standard": 250.0,
    "premium": 1000.0,
    "flagship": 3000.0,
}


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def runway_floor_days() -> float:
    """Below this many days of runway, design drops to conservative (protect runway)."""
    return _fenv("SAMUS_DESIGN_RUNWAY_FLOOR_DAYS", 90.0)


def design_allocation_pct() -> float:
    """Fraction of surplus-above-floor earmarked for design (clamped to [0,1])."""
    return min(1.0, max(0.0, _fenv("SAMUS_DESIGN_ALLOCATION_PCT", 0.15)))


@dataclass(frozen=True)
class FinancialHealth:
    """The slice of finance the anchor consumes (best-effort; fields may be None)."""

    available_balance_usd: Optional[float]
    monthly_burn_usd: Optional[float]
    days_of_runway: Optional[float]   # None = unknown OR infinite (zero burn)
    mrr_usd: Optional[float]
    runway_alert: bool
    ok: bool                          # False = read failed -> fail-safe conservative
    error: Optional[str] = None


@dataclass(frozen=True)
class DesignBudget:
    """The correlated health anchor's output - the design headroom for this pass."""

    tier: str
    rank: int
    design_funds_usd: float           # liquid funds designated to design
    spend_cap_usd: float              # max design spend THIS cycle
    stability_score: float            # 0-1
    healthy_streak: int               # consecutive healthy passes (drives the ratchet)
    unlocked: tuple[str, ...]         # cumulative capabilities at this tier
    rationale: str
    health: dict[str, Any] = field(default_factory=dict)

    def allows(self, capability: str) -> bool:
        return capability in self.unlocked


def read_financial_health() -> FinancialHealth:
    """Best-effort read of Samus finance (runway + MRR). Never raises.

    A failed read returns ``ok=False`` + ``runway_alert=True`` so the policy
    fails safe to the conservative tier rather than inventing headroom.
    """
    try:
        from backend.finance.service import get_runway

        runway = get_runway()
        balance = getattr(runway, "available_balance_usd", None)
        burn = getattr(runway, "total_monthly_burn_usd", None)
        days = getattr(runway, "days_of_runway", None)
        alert = bool(getattr(runway, "alert_triggered", False))

        mrr: Optional[float] = None
        try:
            from backend.finance.models import SnapshotRequest
            from backend.finance.service import get_snapshot

            snap = get_snapshot(SnapshotRequest())
            mrr = float(getattr(snap, "mrr_usd", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 - MRR is supplementary; runway is the anchor
            mrr = None

        return FinancialHealth(
            available_balance_usd=float(balance) if balance is not None else None,
            monthly_burn_usd=float(burn) if burn is not None else None,
            days_of_runway=float(days) if days is not None else None,
            mrr_usd=mrr,
            runway_alert=alert,
            ok=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe to conservative
        _LOG.warning("design_budget financial read failed: %s", exc)
        return FinancialHealth(
            available_balance_usd=None,
            monthly_burn_usd=None,
            days_of_runway=None,
            mrr_usd=None,
            runway_alert=True,
            ok=False,
            error=str(exc),
        )


def _designated_design_funds(h: FinancialHealth) -> float:
    """Liquid funds earmarked for design = a fraction of surplus above the floor.

    surplus = balance - (daily_burn * runway_floor_days). Only cash beyond
    keeping the lights on for the floor period is free to invest in design; that
    surplus is scaled by the design allocation fraction. Never negative.
    """
    if not h.ok or h.available_balance_usd is None:
        return 0.0
    daily_burn = (h.monthly_burn_usd or 0.0) / 30.0
    floor_reserve = daily_burn * runway_floor_days()
    surplus = h.available_balance_usd - floor_reserve
    if surplus <= 0:
        return 0.0
    return round(surplus * design_allocation_pct(), 2)


def _stability_score(h: FinancialHealth) -> float:
    """0-1 stability from runway depth + no alert. Unknown / alert -> 0."""
    if not h.ok or h.runway_alert:
        return 0.0
    days = h.days_of_runway
    if days is None:
        return 1.0   # ok + zero burn = infinite runway = maximally stable
    # full stability at ~2x the floor (e.g. 180 days), linear below.
    return max(0.0, min(1.0, days / (2.0 * runway_floor_days())))


def _eligible_rank(design_funds: float) -> int:
    """Highest tier rank whose funds-floor ``design_funds`` clears."""
    rank = 0
    for i, tier in enumerate(TIERS):
        if design_funds >= _TIER_FUNDS_FLOOR[tier]:
            rank = i
    return rank


def _spend_cap(tier: str, design_funds: float) -> float:
    """Per-cycle design spend cap: the tier ceiling, never exceeding the earmark."""
    return round(min(_TIER_SPEND_CAP.get(tier, 0.0), design_funds), 2)


def compute_design_budget(
    health: FinancialHealth,
    prior_tier: str = "conservative",
    prior_streak: int = 0,
) -> DesignBudget:
    """Apply the anchor policy + hysteresis to produce the current design budget.

    Pure (no I/O) so it is fully testable. ``prior_tier`` / ``prior_streak`` come
    from the last house-growth scorecard, giving the ratchet its memory: it rises
    at most one step per pass and only when the tier's sustained-stability streak
    is met; it drops straight to conservative when runway is unsafe.
    """
    prior_rank = TIERS.index(prior_tier) if prior_tier in TIERS else 0
    stability = _stability_score(health)
    healthy = (
        health.ok
        and not health.runway_alert
        and (health.days_of_runway is None or health.days_of_runway >= runway_floor_days())
    )
    streak = (prior_streak + 1) if healthy else 0
    design_funds = _designated_design_funds(health)
    eligible = _eligible_rank(design_funds)

    if not healthy:
        new_rank = 0
        reason = "runway below floor / alert / unknown -> conservative (protect runway)"
    else:
        target = min(eligible, prior_rank + 1)   # rise at most one step per pass
        if target > prior_rank:
            step_tier = TIERS[target]
            if streak >= _TIER_STREAK_REQUIRED[step_tier]:
                new_rank = target
                reason = (
                    f"sustained stability (streak {streak}) + ${design_funds:,.0f} "
                    f"designated -> ratchet up to {step_tier}"
                )
            else:
                new_rank = prior_rank
                reason = (
                    f"${design_funds:,.0f} qualifies for {step_tier} but streak "
                    f"{streak} < {_TIER_STREAK_REQUIRED[step_tier]} required; holding {prior_tier}"
                )
        elif eligible < prior_rank:
            new_rank = prior_rank - 1   # funds fell below current tier: step down gracefully
            reason = f"${design_funds:,.0f} designated fell below {prior_tier}; step down"
        else:
            new_rank = prior_rank
            reason = f"holding {prior_tier} (${design_funds:,.0f} designated, streak {streak})"

    tier = TIERS[new_rank]
    unlocked = tuple(c for i in range(new_rank + 1) for c in _TIER_CAPABILITIES[TIERS[i]])
    return DesignBudget(
        tier=tier,
        rank=new_rank,
        design_funds_usd=design_funds,
        spend_cap_usd=_spend_cap(tier, design_funds),
        stability_score=round(stability, 3),
        healthy_streak=streak,
        unlocked=unlocked,
        rationale=reason,
        health={
            "balance_usd": health.available_balance_usd,
            "monthly_burn_usd": health.monthly_burn_usd,
            "days_of_runway": health.days_of_runway,
            "mrr_usd": health.mrr_usd,
            "runway_alert": health.runway_alert,
            "ok": health.ok,
        },
    )


def current_design_budget(
    prior_tier: str = "conservative", prior_streak: int = 0
) -> DesignBudget:
    """Read finance (best-effort) + compute the design budget in one call."""
    return compute_design_budget(read_financial_health(), prior_tier, prior_streak)


__all__ = [
    "TIERS",
    "FinancialHealth",
    "DesignBudget",
    "read_financial_health",
    "compute_design_budget",
    "current_design_budget",
    "runway_floor_days",
    "design_allocation_pct",
]
