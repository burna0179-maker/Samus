"""Hustleforge Client Zero - the recurring self-improvement tick.

One best-effort observe -> decide -> record pass over the Hustleforge house
account (``backend.growth.house_account``), modeled on
``backend.gateway.control_tick``: it calls workcell service functions in-process,
captures per-stage failures without sinking the pass, appends a scorecard row,
and returns a snapshot. Idempotent + safe to run on any cadence.

WHAT IT DOES (and deliberately does NOT)
  OBSERVE  measures HF's live web presence - runs the REAL ``seo.audit_site`` on
           the house domain (seo_score + issues) and loads the Brand Brain.
  DECIDE   turns the snapshot + brand profile into a prioritized list of
           improvement hypotheses (top SEO fixes when below target; a rotating
           content asset for an under-served pillar/keyword) - the
           "scan -> hypotheses/week" engine.
  RECORD   appends a scorecard row to the house-growth ledger so the trajectory
           is MEASURABLE over time (``scorecard_trend``), not just "it ran".

It RECORDS a prioritized improvement queue; it does NOT fire outward actions.
Executing a hypothesis (publishing content, applying SEO fixes, posting social)
stays behind the existing website / cash_engine / scheduler outward gates, and
the *quality* of each generated asset is the Fable enrichment layer. That keeps
this loop default-on and safe on any cadence.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from backend.common.dates import iso_now

from . import design_budget, house_account, house_growth_ledger

_LOG = logging.getLogger("samus.growth.house_growth_tick")

# SEO score (0-100) at/above which we stop queueing SEO-fix hypotheses.
_SEO_TARGET: int = 85
# Severity ranking so the worst issues surface first.
_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}
# Max improvement hypotheses surfaced per pass (the weekly discipline).
_MAX_HYPOTHESES: int = 5
# Content channels a content-asset hypothesis can fan out to.
_CONTENT_CHANNELS: tuple[str, ...] = ("wordpress_blog", "instagram", "facebook", "tiktok")


def house_growth_enabled() -> bool:
    """House growth loop is ON by default; ``SAMUS_HOUSE_GROWTH_ENABLED=0`` disables.

    Env-gated (not a settings field) so it can be flipped without a config
    reload, matching the finance upsell-loop idiom; default-on per the
    additive-feature posture.
    """
    raw = os.getenv("SAMUS_HOUSE_GROWTH_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _issue_dict(issue: Any) -> dict[str, str]:
    """Normalize a SeoIssue (severity/category/message) to a plain dict."""
    return {
        "severity": str(getattr(issue, "severity", "") or ""),
        "category": str(getattr(issue, "category", "") or ""),
        "message": str(getattr(issue, "message", getattr(issue, "title", "")) or ""),
    }


def _observe_seo(domain: str, keywords: list[str]) -> dict[str, Any]:
    """OBSERVE: a real SEO audit of the house domain. Best-effort; never raises."""
    try:
        from backend.seo.models import AuditRequest
        from backend.seo.service import audit_site

        result = audit_site(AuditRequest(url=domain, keywords=keywords[:10]))
        issues = [_issue_dict(i) for i in (result.issues or [])]
        issues.sort(key=lambda d: _SEVERITY_RANK.get(d["severity"], 9))
        return {
            "ok": True,
            "seo_score": int(result.seo_score),
            "issue_count": len(issues),
            "issues": issues,
        }
    except Exception as exc:  # noqa: BLE001 - best-effort observe, never blocks
        _LOG.warning("house tick OBSERVE seo failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "seo_score": None,
            "issue_count": 0,
            "issues": [],
        }


def _decide(
    seo_obs: dict[str, Any],
    profile: house_account.BrandProfile,
    pass_index: int,
    budget: design_budget.DesignBudget,
) -> list[dict[str, Any]]:
    """DECIDE: prioritized improvement hypotheses from snapshot + Brand Brain.

    Design-upgrade hypotheses (animation, 3D) appear ONLY when the financial
    anchor (``budget``) has unlocked the headroom for them - the correlation
    between liquid surplus and website ambition made concrete.
    """
    hypotheses: list[dict[str, Any]] = []

    # 1) SEO fixes - only when measurably below target; surface worst-first.
    score = seo_obs.get("seo_score")
    if seo_obs.get("ok") and score is not None and score < _SEO_TARGET:
        for issue in seo_obs.get("issues", [])[:3]:
            hypotheses.append(
                {
                    "channel": "seo",
                    "kind": "seo_fix",
                    "priority": issue.get("severity", ""),
                    "rationale": f"SEO score {score} < target {_SEO_TARGET}",
                    "detail": issue.get("message", ""),
                    "category": issue.get("category", ""),
                    "target": profile.domain,
                }
            )

    # 2) Content asset - rotate through pillars/keywords so coverage broadens
    #    week over week (the traffic engine: "more media -> more traffic").
    pillars = list(profile.content_pillars)
    keywords = list(profile.target_keywords)
    if pillars:
        pillar = pillars[pass_index % len(pillars)]
        keyword = keywords[pass_index % len(keywords)] if keywords else ""
        hypotheses.append(
            {
                "channel": "content",
                "kind": "content_asset",
                "priority": "medium",
                "rationale": "weekly pillar coverage (traffic engine)",
                "detail": pillar,
                "target_keyword": keyword,
                "channels": [c for c in profile.channels if c in _CONTENT_CHANNELS],
            }
        )

    # 3) Design upgrade - gated by the correlated health anchor. Ambition only
    #    appears once sustained financial surplus has unlocked it; conservative
    #    / standard tiers queue nothing here (protect runway).
    if budget.allows("threed_renders"):
        hypotheses.append(
            {
                "channel": "website",
                "kind": "design_upgrade",
                "priority": "high",
                "rationale": (
                    f"design tier '{budget.tier}' unlocked by ${budget.design_funds_usd:,.0f} "
                    f"designated funds (healthy streak {budget.healthy_streak})"
                ),
                "detail": (
                    f"Elevate {profile.domain} with a 3D hero render + hero animation so it "
                    f"reads like a top-tier tech firm's best work."
                ),
                "spend_cap_usd": budget.spend_cap_usd,
                "capabilities": list(budget.unlocked),
            }
        )
    elif budget.allows("scroll_animations"):
        hypotheses.append(
            {
                "channel": "website",
                "kind": "design_upgrade",
                "priority": "medium",
                "rationale": (
                    f"design tier '{budget.tier}' unlocked by ${budget.design_funds_usd:,.0f} "
                    f"designated funds (healthy streak {budget.healthy_streak})"
                ),
                "detail": f"Elevate {profile.domain} with rich scroll animations + motion design.",
                "spend_cap_usd": budget.spend_cap_usd,
                "capabilities": list(budget.unlocked),
            }
        )

    return hypotheses[:_MAX_HYPOTHESES]


def run_house_growth_tick() -> dict[str, Any]:
    """Run ONE Client-Zero improvement pass. Best-effort, idempotent, default-on.

    Returns a snapshot dict (also persisted as a scorecard row). When disabled
    via ``SAMUS_HOUSE_GROWTH_ENABLED=0`` it returns a skip marker without
    observing.
    """
    ts = iso_now()
    if not house_growth_enabled():
        return {"ts": ts, "enabled": False, "skipped": "house_growth_disabled"}

    profile = house_account.load_brand_profile()

    # Prior-pass memory from the last scorecard: pass index (content rotation)
    # plus design tier + healthy streak (the financial anchor's hysteresis).
    last = house_growth_ledger.recent_scorecards(limit=1)["scorecards"]
    prior = last[-1] if last else {}
    try:
        pass_index = int(prior.get("pass_index", -1)) + 1 if prior else 0
    except (TypeError, ValueError):
        pass_index = 0
    prior_tier = str(prior.get("design_tier", "conservative"))
    try:
        prior_streak = int(prior.get("design_healthy_streak", 0))
    except (TypeError, ValueError):
        prior_streak = 0

    # The correlated health anchor: website design ambition <- financial surplus.
    budget = design_budget.current_design_budget(prior_tier, prior_streak)
    seo_obs = _observe_seo(profile.domain, list(profile.target_keywords))
    hypotheses = _decide(seo_obs, profile, pass_index, budget)

    scorecard = {
        "ts": ts,
        "account": house_account.HOUSE_ACCOUNT_ID,
        "domain": profile.domain,
        "pass_index": pass_index,
        "seo_ok": seo_obs.get("ok", False),
        "seo_score": seo_obs.get("seo_score"),
        "seo_issue_count": seo_obs.get("issue_count", 0),
        "hypothesis_count": len(hypotheses),
        "hypotheses": hypotheses,
        "goal_monthly_usd": profile.revenue_goal_monthly_usd,
        # correlated health anchor (website design ambition <- financial surplus);
        # design_tier/streak persist here so the next pass's ratchet has memory.
        "design_tier": budget.tier,
        "design_rank": budget.rank,
        "design_funds_usd": budget.design_funds_usd,
        "design_spend_cap_usd": budget.spend_cap_usd,
        "design_healthy_streak": budget.healthy_streak,
        "financial_health": budget.health,
    }
    recorded = house_growth_ledger.record_scorecard(scorecard)

    return {
        "ts": ts,
        "enabled": True,
        "account": house_account.HOUSE_ACCOUNT_ID,
        "domain": profile.domain,
        "pass_index": pass_index,
        "seo": seo_obs,
        "hypotheses": hypotheses,
        "recorded": recorded,
        "seo_trend": house_growth_ledger.scorecard_trend("seo_score"),
        "design_budget": {
            "tier": budget.tier,
            "design_funds_usd": budget.design_funds_usd,
            "spend_cap_usd": budget.spend_cap_usd,
            "healthy_streak": budget.healthy_streak,
            "unlocked": list(budget.unlocked),
            "rationale": budget.rationale,
        },
        # the anchor's correlation over time: design rank rising with financial health
        "design_tier_trend": house_growth_ledger.scorecard_trend("design_rank"),
    }


__all__ = ["run_house_growth_tick", "house_growth_enabled"]
