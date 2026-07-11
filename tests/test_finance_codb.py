"""CODB loader + runway math — pure function tests."""
from __future__ import annotations


def test_load_default_registry_validates():
    from backend.finance.codb import load_registry
    reg = load_registry()
    assert len(reg.costs) >= 6
    assert reg.revenue_targets.monthly_minimum_usd > 0
    assert reg.revenue_targets.runway_alert_days > 0
    # Every cost has a unique id.
    ids = [c.id for c in reg.costs]
    assert len(ids) == len(set(ids))


def test_load_registry_respects_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom_codb.yaml"
    custom.write_text(
        "costs:\n"
        "  - id: test-cost\n"
        "    name: Test Cost\n"
        "    category: infrastructure\n"
        "    criticality: low\n"
        "    estimated_monthly_usd: 42\n"
        "    notes: smoke\n"
        "revenue_targets:\n"
        "  monthly_minimum_usd: 100\n"
        "  runway_alert_days: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_CODB_REGISTRY_PATH", str(custom))
    from backend.finance.codb import load_registry
    reg = load_registry()
    assert len(reg.costs) == 1
    assert reg.costs[0].id == "test-cost"
    assert reg.costs[0].estimated_monthly_usd == 42
    assert reg.revenue_targets.monthly_minimum_usd == 100


def test_total_monthly_burn():
    from backend.finance.codb import total_monthly_burn
    from backend.finance.models import CodbItem
    items = [
        CodbItem(id="a", name="A", category="infrastructure",
                 criticality="critical", estimated_monthly_usd=10.0),
        CodbItem(id="b", name="B", category="ai",
                 criticality="medium", estimated_monthly_usd=20.5),
        CodbItem(id="c", name="C", category="ai",
                 criticality="low", estimated_monthly_usd=0.75),
    ]
    assert total_monthly_burn(items) == 31.25


def test_burn_by_criticality_groups_correctly():
    from backend.finance.codb import burn_by_criticality
    from backend.finance.models import CodbItem
    items = [
        CodbItem(id="a", name="A", category="infrastructure",
                 criticality="critical", estimated_monthly_usd=10),
        CodbItem(id="b", name="B", category="ai",
                 criticality="critical", estimated_monthly_usd=5),
        CodbItem(id="c", name="C", category="infrastructure",
                 criticality="low", estimated_monthly_usd=3),
    ]
    buckets = burn_by_criticality(items)
    assert buckets["critical"] == 15
    assert buckets["low"] == 3
    assert "medium" not in buckets  # no medium items


def test_burn_by_category_sorted_desc():
    from backend.finance.codb import burn_by_category
    from backend.finance.models import CodbItem
    items = [
        CodbItem(id="a", name="A", category="ai",
                 criticality="high", estimated_monthly_usd=40),
        CodbItem(id="b", name="B", category="infrastructure",
                 criticality="critical", estimated_monthly_usd=10),
        CodbItem(id="c", name="C", category="infrastructure",
                 criticality="medium", estimated_monthly_usd=5),
    ]
    bins = burn_by_category(items)
    # ai = 40, infrastructure = 15
    assert bins[0].category == "ai"
    assert bins[0].total_monthly_usd == 40
    assert bins[0].item_count == 1
    assert bins[1].category == "infrastructure"
    assert bins[1].total_monthly_usd == 15
    assert bins[1].item_count == 2


def test_cuttable_low_first_ordering():
    """Cut order: lowest criticality first; within tier, largest $ first."""
    from backend.finance.codb import cuttable_low_first
    from backend.finance.models import CodbItem
    items = [
        CodbItem(id="critical-small", name="A", category="infrastructure",
                 criticality="critical", estimated_monthly_usd=5),
        CodbItem(id="medium-big", name="B", category="ai",
                 criticality="medium", estimated_monthly_usd=30),
        CodbItem(id="medium-small", name="C", category="ai",
                 criticality="medium", estimated_monthly_usd=10),
        CodbItem(id="low-only", name="D", category="other",
                 criticality="low", estimated_monthly_usd=2),
    ]
    ordered = cuttable_low_first(items)
    ids = [i.id for i in ordered]
    # low first, then medium (biggest first within tier), then critical last
    assert ids == ["low-only", "medium-big", "medium-small", "critical-small"]


def test_compute_runway_normal_case():
    from backend.finance.codb import compute_runway
    r = compute_runway(
        available_balance_usd=300.0,
        total_monthly_burn_usd=150.0,
        runway_alert_days=60,
    )
    # daily burn = 5, runway = 60 days exactly
    assert r.daily_burn_usd == 5.0
    assert r.days_of_runway == 60.0
    # 60 < 60 is False, so alert NOT triggered at exact threshold
    assert r.alert_triggered is False


def test_compute_runway_triggers_alert_when_short():
    from backend.finance.codb import compute_runway
    r = compute_runway(
        available_balance_usd=100.0,
        total_monthly_burn_usd=150.0,
        runway_alert_days=60,
    )
    assert r.days_of_runway is not None
    assert r.days_of_runway < 60
    assert r.alert_triggered is True


def test_compute_runway_zero_burn_is_infinite():
    from backend.finance.codb import compute_runway
    r = compute_runway(
        available_balance_usd=100.0,
        total_monthly_burn_usd=0.0,
        runway_alert_days=60,
    )
    assert r.days_of_runway is None
    assert r.daily_burn_usd == 0.0
    assert r.alert_triggered is False


def test_summarize_full_pipeline(tmp_path, monkeypatch):
    custom = tmp_path / "summary_codb.yaml"
    custom.write_text(
        "costs:\n"
        "  - id: high\n"
        "    name: High\n"
        "    category: ai\n"
        "    criticality: high\n"
        "    estimated_monthly_usd: 100\n"
        "    notes: ''\n"
        "  - id: low\n"
        "    name: Low\n"
        "    category: other\n"
        "    criticality: low\n"
        "    estimated_monthly_usd: 5\n"
        "    notes: ''\n"
        "revenue_targets:\n"
        "  monthly_minimum_usd: 500\n"
        "  runway_alert_days: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_CODB_REGISTRY_PATH", str(custom))
    from backend.finance.codb import load_registry, summarize
    summary = summarize(load_registry(), "2026-05-15T00:00:00Z")
    assert summary.total_monthly_burn_usd == 105
    assert summary.by_criticality == {"high": 100, "low": 5}
    assert summary.cuttable_low_first[0].id == "low"
    assert summary.cuttable_low_first[-1].id == "high"
