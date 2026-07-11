"""Tests for the Hustleforge Client Zero growth engine.

Covers the three additive modules:
  * backend.growth.house_account        — Client Zero identity + Brand Brain
  * backend.growth.house_growth_ledger  — scorecard persistence + trend
  * backend.growth.house_growth_tick     — the observe -> decide -> record loop

The SEO audit is stubbed so these are network-free and deterministic; the
ledger is routed to a tmp path so they never touch the ACL-locked artifact root.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.growth import design_budget, house_account, house_growth_ledger, house_growth_tick


# --- house_account ---------------------------------------------------------

def test_house_client_key_resolves_hustleforge():
    assert house_account.house_client_key() == "hustleforge"


def test_is_house_account_case_insensitive():
    assert house_account.is_house_account("hustleforge")
    assert house_account.is_house_account("  HustleForge ")
    assert not house_account.is_house_account("acme-corp")
    assert not house_account.is_house_account("")


def test_brand_profile_loads_real_seed():
    house_account.load_brand_profile.cache_clear()
    p = house_account.load_brand_profile()
    assert p.account_id == "hustleforge"
    assert p.domain == "https://www.hustleforge.tech"
    assert p.revenue_goal_monthly_usd == 50000.0
    assert p.content_pillars and p.target_keywords
    recurring = p.recurring_rungs()
    assert recurring and all(r.recurring for r in recurring)
    assert any(r.role == "ascension" for r in recurring)


def test_brand_profile_minimal_default_on_missing_yaml(monkeypatch, tmp_path):
    monkeypatch.setattr(house_account, "_PROFILE_PATH", tmp_path / "nope.yaml")
    house_account.load_brand_profile.cache_clear()
    p = house_account.load_brand_profile()
    assert p.domain == "https://www.hustleforge.tech"   # fail-safe floor, never blank
    assert p.account_id == "hustleforge"
    house_account.load_brand_profile.cache_clear()       # don't poison other tests


# --- house_growth_ledger ---------------------------------------------------

@pytest.fixture
def tmp_ledger(monkeypatch, tmp_path):
    """Route the scorecard ledger to a writable tmp path (never the locked root)."""
    monkeypatch.setenv("SAMUS_HOUSE_GROWTH_PATH", str(tmp_path / "house_growth.jsonl"))
    return tmp_path


def test_ledger_record_and_recent(tmp_ledger):
    assert house_growth_ledger.record_scorecard({"seo_score": 70, "pass_index": 0})
    assert house_growth_ledger.record_scorecard({"seo_score": 80, "pass_index": 1})
    view = house_growth_ledger.recent_scorecards()
    assert view["count"] == 2 and view["error"] is None


def test_scorecard_trend_measures_movement(tmp_ledger):
    for i, score in enumerate((60, 68, 75)):
        house_growth_ledger.record_scorecard({"seo_score": score, "pass_index": i})
    t = house_growth_ledger.scorecard_trend("seo_score")
    assert t["direction"] == "up"
    assert t["first"] == 60 and t["last"] == 75
    assert t["delta"] == 15 and t["samples"] == 3


def test_scorecard_trend_insufficient_data(tmp_ledger):
    house_growth_ledger.record_scorecard({"seo_score": 50})
    assert house_growth_ledger.scorecard_trend("seo_score")["direction"] == "insufficient_data"
    assert house_growth_ledger.scorecard_trend("absent_metric")["direction"] == "insufficient_data"


# --- house_growth_tick -----------------------------------------------------

def _stub_audit(score, issues=()):
    def _audit(req):
        return SimpleNamespace(
            url=req.url,
            seo_score=score,
            issues=[SimpleNamespace(severity=s, category=c, message=m) for (s, c, m) in issues],
        )
    return _audit


def test_tick_disabled(monkeypatch, tmp_ledger):
    monkeypatch.setenv("SAMUS_HOUSE_GROWTH_ENABLED", "0")
    out = house_growth_tick.run_house_growth_tick()
    assert out["enabled"] is False and out["skipped"] == "house_growth_disabled"


def test_tick_healthy_site_only_content(monkeypatch, tmp_ledger):
    import backend.seo.service as seo_service
    monkeypatch.setattr(seo_service, "audit_site", _stub_audit(96), raising=True)
    out = house_growth_tick.run_house_growth_tick()
    assert out["enabled"] and out["recorded"]
    assert out["seo"]["seo_score"] == 96
    assert {h["kind"] for h in out["hypotheses"]} == {"content_asset"}   # >= target -> no seo_fix


def test_tick_unhealthy_site_surfaces_seo_fixes(monkeypatch, tmp_ledger):
    import backend.seo.service as seo_service
    monkeypatch.setattr(seo_service, "audit_site", _stub_audit(
        40, issues=[("high", "content", "thin copy"),
                    ("critical", "technical", "missing title")]), raising=True)
    out = house_growth_tick.run_house_growth_tick()
    seo_fixes = [h for h in out["hypotheses"] if h["kind"] == "seo_fix"]
    assert seo_fixes and seo_fixes[0]["priority"] == "critical"   # worst-first ordering


def test_tick_pass_index_increments_and_rotates(monkeypatch, tmp_ledger):
    import backend.seo.service as seo_service
    monkeypatch.setattr(seo_service, "audit_site", _stub_audit(96), raising=True)
    r1 = house_growth_tick.run_house_growth_tick()
    r2 = house_growth_tick.run_house_growth_tick()
    assert r2["pass_index"] == r1["pass_index"] + 1
    c1 = [h for h in r1["hypotheses"] if h["kind"] == "content_asset"][0]["detail"]
    c2 = [h for h in r2["hypotheses"] if h["kind"] == "content_asset"][0]["detail"]
    assert c1 != c2     # content pillar rotation advanced


def test_tick_seo_observe_degrades_without_crashing(monkeypatch, tmp_ledger):
    import backend.seo.service as seo_service

    def _boom(req):
        raise RuntimeError("network down")

    monkeypatch.setattr(seo_service, "audit_site", _boom, raising=True)
    out = house_growth_tick.run_house_growth_tick()
    assert out["seo"]["ok"] is False    # degraded, not crashed
    assert out["recorded"]              # still records a scorecard row


# --- design_budget: the correlated health anchor ---------------------------

@pytest.fixture(autouse=True)
def _conservative_finance_by_default(monkeypatch):
    """Default every test to a fail-safe (conservative) financial read so the
    house-growth tick stays network-free + deterministic; the design tests below
    override this with an explicit healthy read."""
    monkeypatch.setattr(
        design_budget, "read_financial_health",
        lambda: design_budget.FinancialHealth(
            available_balance_usd=None, monthly_burn_usd=None, days_of_runway=None,
            mrr_usd=None, runway_alert=True, ok=False, error="stubbed",
        ),
        raising=True,
    )


def _health(balance, burn, days, alert=False, ok=True, mrr=None):
    return design_budget.FinancialHealth(
        available_balance_usd=balance, monthly_burn_usd=burn, days_of_runway=days,
        mrr_usd=mrr, runway_alert=alert, ok=ok,
    )


def test_design_conservative_on_failed_read():
    b = design_budget.compute_design_budget(_health(None, None, None, ok=False))
    assert b.tier == "conservative" and b.spend_cap_usd == 0.0
    assert b.allows("clean_static") and not b.allows("threed_renders")


def test_design_no_surplus_below_runway_floor():
    # ~30 days of runway at $3000/mo; floor is 90 days -> no surplus, no headroom.
    b = design_budget.compute_design_budget(_health(3000.0, 3000.0, 30.0))
    assert b.design_funds_usd == 0.0 and b.tier == "conservative"


def test_design_funds_are_surplus_above_floor_times_allocation():
    # balance 50000, burn 3000/mo -> daily 100, 90d floor reserve 9000,
    # surplus 41000 * 0.15 allocation = 6150.
    b = design_budget.compute_design_budget(
        _health(50000.0, 3000.0, 500.0), prior_tier="premium", prior_streak=5)
    assert b.design_funds_usd == pytest.approx(6150.0, abs=1.0)


def test_design_ratchets_one_step_per_pass_with_sustained_stability():
    h = _health(100000.0, 2000.0, 1500.0)   # deep runway, large surplus (flagship-eligible)
    b1 = design_budget.compute_design_budget(h, "conservative", 0)
    assert b1.tier == "standard" and b1.healthy_streak == 1
    b2 = design_budget.compute_design_budget(h, b1.tier, b1.healthy_streak)
    assert b2.tier == "premium" and b2.healthy_streak == 2
    b3 = design_budget.compute_design_budget(h, b2.tier, b2.healthy_streak)
    assert b3.tier == "premium"     # flagship needs streak 4; streak is 3 -> hold
    b4 = design_budget.compute_design_budget(h, b3.tier, b3.healthy_streak)
    assert b4.tier == "flagship" and b4.allows("threed_renders") and b4.spend_cap_usd > 0


def test_design_drops_to_conservative_on_runway_alert():
    healthy = _health(100000.0, 2000.0, 1500.0)
    assert design_budget.compute_design_budget(healthy, "flagship", 8).tier == "flagship"
    alerted = _health(500.0, 2000.0, 7.0, alert=True)
    dropped = design_budget.compute_design_budget(alerted, "flagship", 8)
    assert dropped.tier == "conservative" and dropped.healthy_streak == 0


def test_tick_surfaces_3d_design_upgrade_once_anchor_unlocks(monkeypatch, tmp_ledger):
    import backend.seo.service as seo_service
    monkeypatch.setattr(seo_service, "audit_site", _stub_audit(96), raising=True)
    monkeypatch.setattr(design_budget, "read_financial_health",
                        lambda: _health(100000.0, 2000.0, 1500.0), raising=True)
    # First pass ratchets only one step (conservative -> standard): no 3D yet.
    first = house_growth_tick.run_house_growth_tick()
    assert first["design_budget"]["tier"] == "standard"
    assert not any(h["kind"] == "design_upgrade" for h in first["hypotheses"])
    # Sustained stability climbs the ladder to flagship, unlocking the 3D upgrade.
    last = first
    for _ in range(5):
        last = house_growth_tick.run_house_growth_tick()
    assert last["design_budget"]["tier"] == "flagship"
    du = [h for h in last["hypotheses"] if h["kind"] == "design_upgrade"]
    assert du and "3D" in du[0]["detail"] and du[0]["spend_cap_usd"] > 0
