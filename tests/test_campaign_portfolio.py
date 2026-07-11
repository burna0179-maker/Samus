"""Tests for the campaign-portfolio orchestrator.

Selection is pure; orchestration runs injected fake campaigns so nothing real
fires. Core invariant: run as many campaigns as goal pace wants, never more than
monitoring capacity (concurrency ceiling + monitor-cost budget) allows."""

from __future__ import annotations


from backend.cash_engine.campaign_portfolio import (
    Campaign,
    PortfolioDeps,
    run_campaign_portfolio,
    select_campaigns,
    target_campaign_count,
)


def _camp(cid, *, priority=1.0, cost=1.0, eligible=True, produced=1):
    return Campaign(
        campaign_id=cid,
        kind="test",
        priority=priority,
        monitor_cost=cost,
        is_eligible=lambda: eligible,
        actuate=lambda cap: {"initiated": produced, "cap": cap},
        default_cap=5,
    )


# --- target derivation from pace ---------------------------------------------


def test_target_behind_pushes_to_capacity():
    assert target_campaign_count(behind_pace=True, max_concurrent=4) == 4


def test_target_on_pace_is_one():
    assert target_campaign_count(behind_pace=False, max_concurrent=4) == 1


def test_target_unknown_is_moderate():
    assert target_campaign_count(behind_pace=None, max_concurrent=4) == 2


def test_target_zero_capacity():
    assert target_campaign_count(behind_pace=True, max_concurrent=0) == 0


# --- pure selection ----------------------------------------------------------


def test_selects_highest_priority_first():
    camps = [_camp("low", priority=1.0), _camp("high", priority=5.0), _camp("mid", priority=3.0)]
    d = select_campaigns(camps, monitor_budget=99, max_concurrent=2, target_count=2)
    assert d.selected == ["high", "mid"]


def test_target_bounds_selection():
    camps = [_camp(f"c{i}", priority=i) for i in range(5)]
    d = select_campaigns(camps, monitor_budget=99, max_concurrent=5, target_count=2)
    assert len(d.selected) == 2
    assert any(reason == "target_met" for _, reason in d.skipped)


def test_concurrency_ceiling_bounds_selection():
    camps = [_camp(f"c{i}", priority=i) for i in range(5)]
    # target wants 5 but max_concurrent caps at 2
    d = select_campaigns(camps, monitor_budget=99, max_concurrent=2, target_count=5)
    assert len(d.selected) == 2
    assert any(reason == "capacity" for _, reason in d.skipped)


def test_monitor_budget_bounds_selection():
    camps = [
        _camp("a", priority=3, cost=2.0),
        _camp("b", priority=2, cost=2.0),
        _camp("c", priority=1, cost=2.0),
    ]
    # budget 3.0 fits only one 2.0-cost campaign
    d = select_campaigns(camps, monitor_budget=3.0, max_concurrent=9, target_count=9)
    assert d.selected == ["a"]
    assert ("b", "monitor_budget") in d.skipped and ("c", "monitor_budget") in d.skipped
    assert d.monitor_used == 2.0


def test_ineligible_campaigns_excluded():
    camps = [_camp("on", eligible=True), _camp("off", eligible=False)]
    d = select_campaigns(camps, monitor_budget=99, max_concurrent=9, target_count=9)
    assert d.selected == ["on"] and ("off", "ineligible") in d.skipped


def test_eligibility_fault_excludes_not_raises():
    def _boom():
        raise RuntimeError("crm down")

    bad = Campaign(
        "bad",
        kind="t",
        priority=9,
        monitor_cost=1,
        is_eligible=_boom,
        actuate=lambda cap: {"initiated": 1},
    )
    good = _camp("good")
    d = select_campaigns([bad, good], monitor_budget=99, max_concurrent=9, target_count=9)
    assert d.selected == ["good"] and ("bad", "ineligible") in d.skipped


# --- orchestration -----------------------------------------------------------


def test_runs_selected_and_tallies_initiated():
    camps = [_camp("a", priority=2, produced=3), _camp("b", priority=1, produced=4)]
    deps = PortfolioDeps(campaigns=camps, monitor_budget=99, max_concurrent=2)
    out = run_campaign_portfolio(deps=deps, behind_pace=True)  # target=2 -> both
    assert set(out["selected"]) == {"a", "b"}
    assert out["initiated"] == 7  # 3 + 4
    assert out["results"]["a"]["cap"] == 5


def test_on_pace_runs_only_top_campaign():
    camps = [_camp("top", priority=9, produced=2), _camp("other", priority=1, produced=5)]
    deps = PortfolioDeps(campaigns=camps, monitor_budget=99, max_concurrent=4)
    out = run_campaign_portfolio(deps=deps, behind_pace=False)  # target=1
    assert out["selected"] == ["top"] and out["initiated"] == 2


def test_one_campaign_fault_does_not_sink_the_rest():
    def _boom(cap):
        raise RuntimeError("send stack down")

    bad = Campaign(
        "bad",
        kind="t",
        priority=9,
        monitor_cost=1.0,
        is_eligible=lambda: True,
        actuate=_boom,
        count_initiated=lambda r: int(r.get("initiated", 0) or 0),
    )
    good = _camp("good", priority=1, produced=5)
    deps = PortfolioDeps(campaigns=[bad, good], monitor_budget=99, max_concurrent=2)
    out = run_campaign_portfolio(deps=deps, behind_pace=True)
    assert "error" in out["results"]["bad"]
    assert out["results"]["good"]["initiated"] == 5 and out["initiated"] == 5


def test_has_call_list_csv_uses_business_date(monkeypatch, tmp_path):
    """Eligibility must key off the PT business date, not the container's UTC
    date.today() (which is a day ahead all evening PT)."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    from backend.cash_engine.campaign_portfolio import _has_call_list_csv
    from backend.common.us_timezones import business_today

    d = tmp_path / "daily_calls"
    d.mkdir()
    (d / f"call_list_{business_today().isoformat()}.csv").write_text("x", encoding="utf-8")
    assert _has_call_list_csv() is True


def test_default_portfolio_deps_seeds_email_and_voice():
    from backend.cash_engine.campaign_portfolio import default_portfolio_deps

    deps = default_portfolio_deps()
    ids = {c.campaign_id for c in deps.campaigns}
    assert ids == {"email_outreach", "voice_consent_routed"}
    assert deps.max_concurrent >= 2
