"""Tests for the organizational-economics report (Concept 4 from the Samus
Assimilation Plan). Every source reader is injected so the suite is
deterministic and never touches live org_debt / approvals / saturation /
regret state.
"""
from __future__ import annotations

import time

from backend.observability import organizational_economics_report as oer


# ---------------------------------------------------------------------------
# Happy path - all four sources present, non-empty
# ---------------------------------------------------------------------------

def _org_debt_stub():
    return {
        "workcells": [
            {"workcell": "prospecting", "org_debt": 0.7, "karma_health": 0.3, "efficiency": 0.4, "circuit_penalty": 0.5},
            {"workcell": "outreach", "org_debt": 0.5, "karma_health": 0.4, "efficiency": 0.6, "circuit_penalty": 0.2},
            {"workcell": "seo", "org_debt": 0.1, "karma_health": 0.9, "efficiency": 0.9, "circuit_penalty": 0.0},
        ],
        "total_org_debt": 1.3,
        "worst": "prospecting",
    }


def _approvals_pending_stub(now: float):
    return [
        {"id": "a", "status": "pending", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))},
        {"id": "b", "status": "pending", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 2 * 3600))},
        {"id": "c", "status": "pending", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5 * 3600))},
    ]


def _approvals_all_stub(now: float):
    pending = _approvals_pending_stub(now)
    approved = [{"id": "d", "status": "approved"}, {"id": "e", "status": "approved"}]
    rejected = [{"id": "f", "status": "rejected"}]
    expired = [{"id": "g", "status": "expired"}, {"id": "h", "status": "expired"}]
    return {
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "expired": expired,
    }


def _saturation_stub():
    return {"dental": 0.6, "hvac": 0.2, "roofing": 0.4}


def _regret_stub():
    return (2.5, 100.0)  # cumulative regret 2.5, token spend 100


def test_happy_path_all_six_metrics_populated():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        org_debt_reader=_org_debt_stub,
        approvals_pending_reader=lambda: _approvals_pending_stub(now),
        approvals_all_reader=lambda: _approvals_all_stub(now),
        saturation_reader=_saturation_stub,
        regret_reader=_regret_stub,
    )
    assert report.enabled is True
    names = [m.name for m in report.metrics]
    assert names == [
        "coordination_cost",
        "decision_latency",
        "context_switching",
        "cognitive_overhead",
        "approval_friction",
        "communication_entropy",
    ]
    assert all(not m.sources_missing for m in report.metrics)


def test_coordination_cost_uses_total_org_debt():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        org_debt_reader=_org_debt_stub,
    )
    metric = next(m for m in report.metrics if m.name == "coordination_cost")
    assert metric.value == 1.3
    assert "prospecting" in metric.detail
    assert metric.source == "backend.governance.org_debt.org_debt_report"


def test_decision_latency_reports_median_pending_age():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        approvals_pending_reader=lambda: _approvals_pending_stub(now),
    )
    metric = next(m for m in report.metrics if m.name == "decision_latency")
    # ages in hours: 1, 2, 5 -> median 2h
    assert metric.value == 2.0
    assert metric.unit == "hours_median"
    assert "3 pending" in metric.detail


def test_context_switching_reports_stddev_of_debts():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        org_debt_reader=_org_debt_stub,
    )
    metric = next(m for m in report.metrics if m.name == "context_switching")
    # debts [0.7, 0.5, 0.1]; mean 0.4333...; variance ~= 0.06222; stddev ~= 0.2494
    assert metric.value > 0.24
    assert metric.value < 0.26
    assert metric.unit == "stddev_org_debt"


def test_cognitive_overhead_is_mean_saturation_risk():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        saturation_reader=_saturation_stub,
    )
    metric = next(m for m in report.metrics if m.name == "cognitive_overhead")
    # mean of [0.6, 0.2, 0.4] = 0.4
    assert abs(metric.value - 0.4) < 1e-6
    assert "peak 0.6" in metric.detail


def test_approval_friction_is_expired_share():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        approvals_all_reader=lambda: _approvals_all_stub(now),
    )
    metric = next(m for m in report.metrics if m.name == "approval_friction")
    # 2 expired out of 8 total
    assert metric.value == 0.25
    assert metric.unit == "expired_share"


def test_communication_entropy_uses_regret_per_token():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        regret_reader=_regret_stub,
    )
    metric = next(m for m in report.metrics if m.name == "communication_entropy")
    # regret 2.5 / token 100 = 0.025
    assert abs(metric.value - 0.025) < 1e-6
    assert metric.source == "backend.strategy.regret_engine.regret_per_token"


# ---------------------------------------------------------------------------
# Missing-source handling - fail-soft neutrals + sources_missing flag
# ---------------------------------------------------------------------------

def test_missing_org_debt_source_degrades_softly():
    now = 1_000_000.0

    def broken() -> dict:
        raise RuntimeError("org_debt store down")

    report = oer.compute_organizational_economics(
        now_ts=now,
        org_debt_reader=broken,
    )
    coord = next(m for m in report.metrics if m.name == "coordination_cost")
    switch = next(m for m in report.metrics if m.name == "context_switching")
    assert coord.sources_missing is True
    assert coord.value == 0.0
    assert switch.sources_missing is True


def test_missing_saturation_supplier_marks_degraded():
    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        saturation_reader=None,
    )
    metric = next(m for m in report.metrics if m.name == "cognitive_overhead")
    assert metric.sources_missing is True
    assert metric.value == 0.0
    assert "no trials-by-vertical supplier" in metric.detail


def test_missing_regret_supplier_marks_degraded():
    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        regret_reader=None,
    )
    metric = next(m for m in report.metrics if m.name == "communication_entropy")
    assert metric.sources_missing is True
    assert metric.value == 0.0


def test_broken_approvals_reader_degrades_both_metrics():
    def broken_pending() -> list:
        raise RuntimeError("approvals ddb down")

    def broken_all() -> dict:
        raise RuntimeError("approvals ddb down")

    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        approvals_pending_reader=broken_pending,
        approvals_all_reader=broken_all,
    )
    latency = next(m for m in report.metrics if m.name == "decision_latency")
    friction = next(m for m in report.metrics if m.name == "approval_friction")
    assert latency.sources_missing is True
    assert friction.sources_missing is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_pending_queue_yields_zero_latency_without_degrade():
    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        approvals_pending_reader=lambda: [],
    )
    metric = next(m for m in report.metrics if m.name == "decision_latency")
    assert metric.value == 0.0
    assert metric.sources_missing is False
    assert "no pending" in metric.detail


def test_empty_saturation_dict_is_not_degraded():
    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        saturation_reader=lambda: {},
    )
    metric = next(m for m in report.metrics if m.name == "cognitive_overhead")
    assert metric.value == 0.0
    assert metric.sources_missing is False
    assert "no verticals" in metric.detail


def test_empty_workcells_yields_zero_context_switching():
    def empty_debt() -> dict:
        return {"workcells": [], "total_org_debt": 0.0, "worst": None}

    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        org_debt_reader=empty_debt,
    )
    switch = next(m for m in report.metrics if m.name == "context_switching")
    assert switch.value == 0.0
    assert switch.sources_missing is False


def test_approvals_with_only_pending_reports_zero_friction():
    def only_pending() -> dict:
        return {"pending": [{"id": "x", "status": "pending"}], "approved": [], "rejected": [], "expired": []}

    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        approvals_all_reader=only_pending,
    )
    friction = next(m for m in report.metrics if m.name == "approval_friction")
    assert friction.value == 0.0


def test_zero_token_spend_uses_epsilon_and_does_not_raise():
    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        regret_reader=lambda: (0.0, 0.0),
    )
    metric = next(m for m in report.metrics if m.name == "communication_entropy")
    # 0 regret / baseline = 0 - the important behavior is no exception
    assert metric.value == 0.0
    assert metric.sources_missing is False


def test_kill_switch_disables_report(monkeypatch):
    monkeypatch.setenv("SAMUS_ORG_ECONOMICS_REPORT_ENABLED", "0")
    report = oer.compute_organizational_economics(now_ts=1_000_000.0)
    assert report.enabled is False
    assert report.metrics == []


def test_as_dict_shape_matches_metrics():
    now = 1_000_000.0
    report = oer.compute_organizational_economics(
        now_ts=now,
        org_debt_reader=_org_debt_stub,
    )
    dumped = report.as_dict()
    assert dumped["enabled"] is True
    assert dumped["generated_ts"] == now
    assert set(dumped["metrics"].keys()) == {
        "coordination_cost", "decision_latency", "context_switching",
        "cognitive_overhead", "approval_friction", "communication_entropy",
    }
    coord = dumped["metrics"]["coordination_cost"]
    assert coord["value"] == 1.3
    assert coord["source"] == "backend.governance.org_debt.org_debt_report"


def test_summary_line_flags_degraded_count():
    def broken() -> dict:
        raise RuntimeError("boom")

    report = oer.compute_organizational_economics(
        now_ts=1_000_000.0,
        org_debt_reader=broken,
    )
    line = report.summary_line()
    # coordination_cost + context_switching both degraded from org_debt, plus
    # cognitive_overhead + communication_entropy degraded from missing suppliers = 4
    assert "6 metrics" in line
    assert "degraded" in line
