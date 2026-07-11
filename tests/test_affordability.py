"""Tests for the affordability layer + its gating of the campaign portfolio.

Core invariant: pace = min(revenue-urgency, affordability). Being far behind on
revenue NEVER overrides the cash safety reserve — a conserve posture runs only
free channels even when behind_pace says push to the max."""

from __future__ import annotations

from types import SimpleNamespace

from backend.cash_engine.affordability import (
    assess_affordability,
    derive_affordability,
)
from backend.cash_engine.campaign_portfolio import (
    Campaign,
    PortfolioDeps,
    run_campaign_portfolio,
)


# --- posture derivation (pure) -----------------------------------------------


def test_conserve_when_headroom_nonpositive():
    a = derive_affordability(headroom_usd=0.0, available_cash_usd=100.0, invest_min=300)
    assert a.posture == "conserve"
    assert a.allowed_tiers == frozenset({"free"})
    assert a.intensity_factor == 0.3


def test_lean_when_some_headroom():
    a = derive_affordability(headroom_usd=120.0, available_cash_usd=500.0, invest_min=300)
    assert a.posture == "lean" and a.allowed_tiers == frozenset({"free", "low"})


def test_invest_when_ample_headroom():
    a = derive_affordability(headroom_usd=800.0, available_cash_usd=4000.0, invest_min=300)
    assert a.posture == "invest" and "paid" in a.allowed_tiers
    assert a.intensity_factor == 1.0 and a.marketing_budget_usd == 800.0


def test_negative_headroom_is_conserve():
    a = derive_affordability(headroom_usd=-50.0, available_cash_usd=10.0, invest_min=300)
    assert a.posture == "conserve"


# --- assess_affordability (injected reader) ----------------------------------


def test_assess_reads_financials_headroom():
    fin = SimpleNamespace(headroom_usd=500.0, available_cash_usd=2000.0, source="test")
    a = assess_affordability(financials_reader=lambda: fin)
    assert a.headroom_usd == 500.0 and a.posture in {"lean", "invest"}


def test_assess_fault_is_cautious_lean_not_blind_push():
    def _boom():
        raise RuntimeError("finance down")

    a = assess_affordability(financials_reader=_boom)
    assert a.posture == "lean" and a.intensity_factor == 0.5
    assert "paid" not in a.allowed_tiers  # never a blind full push


def test_assess_none_financials_is_cautious():
    a = assess_affordability(financials_reader=lambda: None)
    assert a.posture == "lean" and a.source == "unavailable-cautious"


# --- portfolio gating by affordability ---------------------------------------


def _camp(cid, *, tier, cost=0.0, priority=1.0, produced=1):
    return Campaign(
        campaign_id=cid,
        kind="t",
        priority=priority,
        monitor_cost=1.0,
        is_eligible=lambda: True,
        actuate=lambda cap: {"initiated": produced, "cap": cap},
        default_cap=10,
        cost_tier=tier,
        est_cost_usd=cost,
    )


def _deps():
    return PortfolioDeps(
        campaigns=[
            _camp("vm", tier="free"),
            _camp("email", tier="low", cost=0.5),
            _camp("calls", tier="paid", cost=3.0),
        ],
        monitor_budget=99,
        max_concurrent=4,
    )


def test_conserve_runs_only_free_even_when_behind():
    """behind_pace=True wants to push everything, but conserve posture must hold
    the paid + low channels and run ONLY the free one."""
    afford = derive_affordability(headroom_usd=0.0, available_cash_usd=50.0)
    out = run_campaign_portfolio(deps=_deps(), behind_pace=True, affordability=afford)
    assert out["selected"] == ["vm"]
    assert ("email", "tier_blocked:low") in out["skipped"]
    assert ("calls", "tier_blocked:paid") in out["skipped"]
    assert out["posture"] == "conserve"


def test_lean_runs_free_and_low_not_paid():
    afford = derive_affordability(headroom_usd=100.0, available_cash_usd=400.0, invest_min=300)
    out = run_campaign_portfolio(deps=_deps(), behind_pace=True, affordability=afford)
    assert set(out["selected"]) == {"vm", "email"}
    assert ("calls", "tier_blocked:paid") in out["skipped"]


def test_invest_runs_all_when_behind():
    afford = derive_affordability(headroom_usd=900.0, available_cash_usd=5000.0, invest_min=300)
    out = run_campaign_portfolio(deps=_deps(), behind_pace=True, affordability=afford)
    assert set(out["selected"]) == {"vm", "email", "calls"}


def test_spend_budget_blocks_unaffordable_paid_campaign():
    # invest posture (tiers allow paid) but headroom only $2 < the $3 call cost.
    afford = derive_affordability(headroom_usd=2.0, available_cash_usd=2.0, invest_min=1.0)
    out = run_campaign_portfolio(deps=_deps(), behind_pace=True, affordability=afford)
    assert "calls" not in out["selected"]
    assert ("calls", "spend_budget") in out["skipped"]


def test_intensity_scales_per_campaign_volume():
    # conserve intensity 0.3 -> free campaign runs at round(10*0.3)=3.
    afford = derive_affordability(headroom_usd=0.0, available_cash_usd=0.0)
    out = run_campaign_portfolio(deps=_deps(), behind_pace=True, affordability=afford)
    assert out["results"]["vm"]["cap"] == 3


def test_affordability_caps_revenue_urgency():
    # behind_pace=True wants full capacity (4), conserve caps effective target.
    afford = derive_affordability(headroom_usd=0.0, available_cash_usd=0.0)
    out = run_campaign_portfolio(deps=_deps(), behind_pace=True, affordability=afford)
    assert out["revenue_target"] == 4
    assert out["effective_target"] <= 2  # ceil(4 * 0.3) = 2
    assert out["affordability"]["posture"] == "conserve"
