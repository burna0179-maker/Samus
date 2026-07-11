"""Campaign-portfolio orchestrator — "run as many campaigns as needed to reach
the goal, within the capacity to monitor each efficiently."

The idle-production drive decides WHEN to produce; this decides WHAT and HOW MANY.
A :class:`Campaign` is a named, governed production stream (base cold email, the
consent-routed voice pass, or a specific SKU/offer campaign). Each tick the agent:

  1. derives a TARGET count from goal pace — behind => push to capacity, on/ahead
     => a single momentum campaign, unknown => a moderate middle (:func:`target_campaign_count`);
  2. SELECTS eligible campaigns by priority, greedily, bounded by BOTH the target
     and the monitoring capacity (max concurrent + a monitor-cost budget)
     (:func:`select_campaigns` — pure);
  3. ACTUATES each selected campaign for one bounded pass (:func:`run_campaign_portfolio`).

Selection is pure and fully testable; actuation is injected. Adding a campaign is
registering one more :class:`Campaign` descriptor — the selection math is unchanged.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.cash_engine.campaign_portfolio")


@dataclass
class Campaign:
    """A governed production stream the portfolio may choose to run.

    ``priority`` ranks preference when the agent is behind pace (higher first).
    ``monitor_cost`` is how much monitoring capacity one active pass consumes —
    the "within the ability to monitor each efficiently" bound. ``is_eligible``
    gates the campaign (has candidates / in a valid window / not exhausted).
    ``actuate(cap)`` runs one bounded pass and returns a summary dict;
    ``count_initiated`` extracts how many items it actually produced."""
    campaign_id: str
    kind: str
    priority: float
    monitor_cost: float
    is_eligible: Callable[[], bool]
    actuate: Callable[[int], dict[str, Any]]
    count_initiated: Callable[[dict[str, Any]], int] = lambda r: int(r.get("initiated", 0) or 0)
    default_cap: int = 5
    # Financial cost of one pass. ``cost_tier`` gates by affordability posture:
    # "free" (e.g. voicemail drafts — always allowed), "low" (e.g. LLM-personalized
    # email), "paid" (e.g. live calls / paid media — invest posture only).
    # ``est_cost_usd`` is a rough per-pass dollar estimate for the spend ceiling.
    cost_tier: str = "free"
    est_cost_usd: float = 0.0


@dataclass
class PortfolioDecision:
    selected: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)
    monitor_used: float = 0.0
    monitor_budget: float = 0.0
    target_count: int = 0


def target_campaign_count(*, behind_pace: bool | None, max_concurrent: int) -> int:
    """How many campaigns the agent WANTS to run this tick, from goal pace.

    behind  => push to full capacity; on/ahead => one momentum campaign;
    unknown => a moderate middle. Always clamped to ``[0, max_concurrent]``."""
    if max_concurrent <= 0:
        return 0
    if behind_pace is True:
        return max_concurrent
    if behind_pace is False:
        return 1
    return max(1, max_concurrent // 2)


def select_campaigns(
    campaigns: list[Campaign],
    *,
    monitor_budget: float,
    max_concurrent: int,
    target_count: int,
    allowed_tiers: Optional[frozenset] = None,
    spend_budget: float = float("inf"),
) -> PortfolioDecision:
    """Pure selection. Rank eligible campaigns by priority (desc), tie-break by
    cheaper monitor_cost then id; greedily select while under the target AND the
    concurrency ceiling AND the monitor budget AND (when affordability is applied)
    the campaign's cost tier is allowed AND the running dollar spend stays within
    ``spend_budget``. ``allowed_tiers=None`` means no financial gating (permissive
    — a caller that opted out of affordability). Every non-pick gets a reason."""
    decision = PortfolioDecision(monitor_budget=monitor_budget, target_count=target_count)
    ceiling = min(target_count, max_concurrent)
    spend_used = 0.0

    eligible: list[Campaign] = []
    for c in campaigns:
        try:
            ok = bool(c.is_eligible())
        except Exception as exc:  # noqa: BLE001 — an eligibility fault excludes it
            _LOG.warning("campaign %s eligibility check failed: %s", c.campaign_id, exc)
            ok = False
        if ok:
            eligible.append(c)
        else:
            decision.skipped.append((c.campaign_id, "ineligible"))

    ranked = sorted(eligible, key=lambda c: (-c.priority, c.monitor_cost, c.campaign_id))
    for c in ranked:
        # Affordability gates come FIRST — we never even consider a campaign we
        # can't afford, regardless of how far behind on revenue we are.
        if allowed_tiers is not None and c.cost_tier not in allowed_tiers:
            decision.skipped.append((c.campaign_id, f"tier_blocked:{c.cost_tier}"))
            continue
        if spend_used + c.est_cost_usd > spend_budget:
            decision.skipped.append((c.campaign_id, "spend_budget"))
            continue
        if len(decision.selected) >= ceiling:
            reason = "target_met" if ceiling >= target_count else "capacity"
            decision.skipped.append((c.campaign_id, reason))
            continue
        if decision.monitor_used + c.monitor_cost > monitor_budget:
            decision.skipped.append((c.campaign_id, "monitor_budget"))
            continue
        decision.selected.append(c.campaign_id)
        decision.monitor_used += c.monitor_cost
        spend_used += c.est_cost_usd
    return decision


@dataclass
class PortfolioDeps:
    campaigns: list[Campaign]
    monitor_budget: float = 3.0
    max_concurrent: int = 4


def run_campaign_portfolio(
    *,
    deps: PortfolioDeps,
    behind_pace: bool | None = None,
    affordability: Any = None,
) -> dict[str, Any]:
    """Select + actuate the campaign portfolio for one tick. Never raises.

    Intensity is ``min(revenue-urgency, affordability)``: ``behind_pace`` sets how
    many campaigns the revenue gap WANTS; ``affordability`` (from the CODB
    reasoner's headroom) caps how many + which cost tiers we can responsibly run,
    and scales per-campaign volume by its posture. ``affordability=None`` means no
    financial gating (permissive — a caller that opted out)."""
    revenue_target = target_campaign_count(
        behind_pace=behind_pace, max_concurrent=deps.max_concurrent)

    if affordability is None:
        allowed_tiers: Optional[frozenset] = None
        spend_budget = float("inf")
        intensity = 1.0
        effective_target = revenue_target
        posture = "unbounded"
    else:
        intensity = float(affordability.intensity_factor)
        allowed_tiers = affordability.allowed_tiers
        spend_budget = float(affordability.marketing_budget_usd)
        afford_target = max(0, math.ceil(deps.max_concurrent * intensity))
        effective_target = min(revenue_target, afford_target)
        posture = affordability.posture

    decision = select_campaigns(
        deps.campaigns, monitor_budget=deps.monitor_budget,
        max_concurrent=deps.max_concurrent, target_count=effective_target,
        allowed_tiers=allowed_tiers, spend_budget=spend_budget,
    )
    by_id = {c.campaign_id: c for c in deps.campaigns}
    results: dict[str, Any] = {}
    initiated = 0
    for cid in decision.selected:
        c = by_id[cid]
        cap = max(1, round(c.default_cap * intensity))  # volume scaled by posture
        try:
            r = c.actuate(cap) or {}
            results[cid] = r
            initiated += int(c.count_initiated(r) or 0)
        except Exception as exc:  # noqa: BLE001 — one campaign fault never sinks the rest
            _LOG.warning("campaign %s actuation failed: %s", cid, exc)
            results[cid] = {"error": str(exc)}

    _LOG.info(
        "portfolio: posture=%s revenue_target=%d effective=%d selected=%s "
        "initiated=%d monitor=%.1f/%.1f spend<=%.2f",
        posture, revenue_target, effective_target, decision.selected, initiated,
        decision.monitor_used, deps.monitor_budget, spend_budget,
    )
    return {
        "channel": "portfolio",
        "posture": posture,
        "revenue_target": revenue_target,
        "effective_target": effective_target,
        "target_count": effective_target,  # back-compat alias
        "affordability": affordability.to_dict() if affordability is not None else None,
        "selected": decision.selected,
        "skipped": decision.skipped,
        "monitor_used": decision.monitor_used,
        "monitor_budget": deps.monitor_budget,
        "initiated": initiated,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Default portfolio — seeds the two live governed channels as campaigns. Adding
# a SKU/offer campaign is one more Campaign entry here; the math is unchanged.
# ---------------------------------------------------------------------------

def _has_call_list_csv() -> bool:
    try:
        from backend.common import storage
        from backend.common.us_timezones import business_today
        return (storage.root() / "daily_calls" /
                f"call_list_{business_today().isoformat()}.csv").is_file()
    except Exception:  # noqa: BLE001
        return False


def _funnel_allows_checkout_push() -> bool:
    """Consult checkout-funnel health before a campaign that pushes buy links.

    A campaign whose emails carry payment links converts ZERO when those
    links are archived — the send burns CODB (SendGrid + LLM tokens) for
    nothing. The gate reads a cached snapshot (no network on the tick) and is
    WIRED-DORMANT: unarmed it always returns True and logs a warning when the
    funnel is degraded, so Samus is conscious of the state without production
    being silenced; ``SAMUS_FUNNEL_GATE_ENABLED=1`` arms enforcement.
    Fail-open on any error — a broken health check must never stop revenue.
    Scoped with ``consider_wp=False``: a sent email carries CATALOG links, so
    the gate keys on catalog/live-code staleness, NOT on a stale WordPress-page
    link (a different surface — the brief + WP remediation tool own that). This
    keeps an unrelated website placeholder from halting the whole email channel.
    """
    try:
        from backend.catalog.funnel_health import funnel_gate
        return funnel_gate(consider_wp=False).allowed
    except Exception:  # noqa: BLE001
        return True


def default_portfolio_deps(affordability: Any = None) -> PortfolioDeps:
    from backend.cash_engine.production_actuator import (
        _DEFAULT_CAP_EMAIL,
        _DEFAULT_CAP_VOICE,
        default_production_deps,
        run_email_channel,
        run_voice_channel,
    )

    pdeps = default_production_deps()
    # A live governed dial is a PAID action (ADR-018): permit it only when the
    # CODB affordability posture allows the "paid" tier (invest). Under
    # conserve/lean the voice pass still runs but routes every prospect to a
    # FREE voicemail draft — so call VOLUME is bounded by the reasoner's
    # headroom, not a separate cap. affordability=None (a caller that opted out
    # of financial gating) permits live dial.
    try:
        allowed = getattr(affordability, "allowed_tiers", None)
        live_dial_allowed = (allowed is None) or ("paid" in allowed)
    except Exception:  # noqa: BLE001
        live_dial_allowed = False
    email = Campaign(
        campaign_id="email_outreach", kind="email", priority=1.0, monitor_cost=1.0,
        # Email pushes checkout links — a stale funnel makes every send a
        # zero-conversion CODB burn, so eligibility consults funnel health
        # (wired-dormant; see _funnel_allows_checkout_push).
        is_eligible=lambda: _has_call_list_csv() and _funnel_allows_checkout_push(),
        actuate=lambda cap: run_email_channel(pdeps, cap),
        count_initiated=lambda r: int(r.get("sent", 0) or 0),
        default_cap=_DEFAULT_CAP_EMAIL,
        # LLM-personalized email costs tokens -> "low" tier; held under conserve.
        cost_tier="low", est_cost_usd=0.50,
    )
    voice = Campaign(
        campaign_id="voice_consent_routed", kind="voice", priority=1.2, monitor_cost=1.5,
        is_eligible=lambda: bool(pdeps.voice_candidates()),
        actuate=lambda cap: run_voice_channel(pdeps, cap, live_dial_allowed=live_dial_allowed),
        count_initiated=lambda r: int(r.get("dialed", 0) or 0) + int(r.get("drafted", 0) or 0),
        default_cap=_DEFAULT_CAP_VOICE,
        # The voice campaign ITSELF stays "free"-tier so it always runs to
        # produce the zero-cost voicemail-draft floor even under conserve; the
        # PAID live-dial escalation inside it is gated by live_dial_allowed
        # above (the CODB affordability ceiling).
        cost_tier="free", est_cost_usd=0.0,
    )
    return PortfolioDeps(campaigns=[email, voice], monitor_budget=3.0, max_concurrent=4)
