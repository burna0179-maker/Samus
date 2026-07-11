"""Executive command center (HOTL Tranche 4) — the aggregate + gateway routes +
morning-brief integration.

Isolation: tmp goals/plans/approvals/business-events/state paths per test.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from backend.common import approvals, business_events, decision_record as dr
from backend.planning import command_center as cc
from backend.planning import goal_tree, planner, store
from backend.planning.models import HORIZON_DAY, Goal


@pytest.fixture
def iso_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setenv("SAMUS_PLANS_PATH", str(tmp_path / "plans.json"))
    monkeypatch.setenv("SAMUS_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_ROI_ROLLUP_PATH", str(tmp_path / "roi.json"))
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    return tmp_path


# --- aggregate shape -------------------------------------------------------

def test_build_command_center_has_all_sections(iso_env):
    agg = cc.build_command_center()
    assert agg["ok"] is True
    for section in (
        "what_happened", "why", "running_now", "needs_approval",
        "economics", "health",
    ):
        assert section in agg


def test_command_center_what_happened_digests_events(iso_env):
    for _ in range(3):
        business_events.emit_business_event(
            business_events.LEAD_CREATED, workcell="intake",
        )
    business_events.emit_business_event(
        business_events.EMAIL_SENT, workcell="outreach",
    )
    agg = cc.build_command_center()
    wh = agg["what_happened"]
    assert wh["event_count"] == 4
    assert wh["by_type"]["lead.created"] == 3
    assert wh["by_type"]["email.sent"] == 1
    assert len(wh["recent"]) == 4


def test_command_center_why_surfaces_decisions(iso_env):
    dr.record_decision("planner", "generated a plan", workcell="planning")
    agg = cc.build_command_center()
    decisions = agg["why"]["decisions"]
    assert decisions
    assert decisions[0]["why"] == "generated a plan"


def test_command_center_running_now_counts_plans(iso_env, monkeypatch):
    monkeypatch.setattr(
        "backend.cash_engine.affordability.assess_affordability",
        lambda **_: type("P", (), {"posture": "invest"})(),
    )
    goal = Goal(id="g1", horizon=HORIZON_DAY, target_metric="leads_created",
                target_value=8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)
    agg = cc.build_command_center()
    running = agg["running_now"]
    assert running["plan_count"] >= 1
    assert running["goal_count"] >= 1


def test_command_center_needs_approval_lists_pending(iso_env):
    approvals.create_approval("replan", {"goal_id": "g1"}, risk_level="normal")
    approvals.create_approval("stake", {"opportunity_id": "o1"}, risk_level="high")
    agg = cc.build_command_center()
    needs = agg["needs_approval"]
    assert needs["pending_count"] == 2
    assert needs["emergency_count"] == 1  # the high-risk one is emergency severity


def test_command_center_economics_section(iso_env):
    agg = cc.build_command_center(day="2026-07-06")
    # rollup present (even if all-zero on an empty ledger), never a raise
    assert "rollup" in agg["economics"] or "error" in agg["economics"]


def test_command_center_health_section(iso_env):
    agg = cc.build_command_center()
    health = agg["health"]
    assert "health" in health
    # 4-state aggregate has a state key
    assert "state" in health["health"]


def test_command_center_narrows_to_prospect(iso_env):
    business_events.emit_business_event(
        business_events.LEAD_CREATED, workcell="intake", prospect_id="p1",
    )
    business_events.emit_business_event(
        business_events.LEAD_CREATED, workcell="intake", prospect_id="p2",
    )
    agg = cc.build_command_center(prospect_id="p1")
    assert agg["what_happened"]["event_count"] == 1


def test_build_command_center_never_raises(iso_env, monkeypatch):
    # Break one section source; the aggregate still returns ok with an error
    # note on that section only.
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(business_events, "read_events", boom)
    agg = cc.build_command_center()
    assert agg["ok"] is True
    assert "error" in agg["what_happened"]


# --- gateway routes --------------------------------------------------------

@pytest.fixture
def client(iso_env, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.gateway.app import create_app

    monkeypatch.setenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", "1")
    app = create_app()
    return TestClient(app)


def test_route_autonomy_plan_get(client, monkeypatch):
    monkeypatch.setattr(
        "backend.cash_engine.affordability.assess_affordability",
        lambda **_: type("P", (), {"posture": "invest"})(),
    )
    goal = Goal(id="g1", horizon=HORIZON_DAY, target_metric="leads_created",
                target_value=8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)
    resp = client.get("/autonomy/plan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["plan_count"] >= 1


def test_route_admin_decisions_list_and_detail(client):
    rec = dr.record_decision(
        "planner", "why here", workcell="planning",
        alternatives_considered=["alt"],
    )
    resp = client.get("/admin/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] >= 1
    # drill-down
    detail = client.get(f"/admin/decisions/{rec.decision_id}")
    assert detail.status_code == 200
    dbody = detail.json()
    assert dbody["ok"] is True
    assert dbody["decision"]["why"] == "why here"


def test_route_admin_decisions_detail_unknown(client):
    resp = client.get("/admin/decisions/nonexistent")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_route_admin_approvals_list_and_decide(client):
    row = approvals.create_approval("replan", {"goal_id": "g1"},
                                    risk_level="normal")
    resp = client.get("/admin/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    # decide it
    dec = client.post(
        "/admin/approvals/decide",
        json={"approval_id": row["id"], "decision": "approved"},
    )
    assert dec.status_code == 200
    assert dec.json()["ok"] is True
    assert dec.json()["approval"]["status"] == "approved"


def test_route_admin_approvals_batch(client):
    r1 = approvals.create_approval("replan", {"g": 1}, risk_level="normal")
    r2 = approvals.create_approval("replan", {"g": 2}, risk_level="normal")
    resp = client.post(
        "/admin/approvals/decide",
        json={"approval_ids": [r1["id"], r2["id"]]},
    )
    assert resp.status_code == 200
    batch = resp.json()["batch"]
    assert set(batch["approved"]) == {r1["id"], r2["id"]}


def test_route_admin_approvals_decide_missing_args(client):
    resp = client.post("/admin/approvals/decide", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_route_command_center(client):
    business_events.emit_business_event(
        business_events.LEAD_CREATED, workcell="intake",
    )
    resp = client.get("/admin/command_center")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "what_happened" in body
    assert "economics" in body


# --- morning brief integration ---------------------------------------------

def test_morning_brief_renders_command_center(iso_env, monkeypatch):
    import backend.morning as morning

    monkeypatch.setattr(
        "backend.cash_engine.affordability.assess_affordability",
        lambda **_: type("P", (), {"posture": "invest"})(),
    )
    goal_tree.seed_goal_tree(today=_dt.date(2026, 1, 1))
    brief = morning.render_briefing()
    assert "COMMAND CENTER" in brief


def test_render_command_center_returns_lines(iso_env, monkeypatch):
    import backend.morning as morning

    approvals.create_approval("replan", {"g": 1}, risk_level="high")
    lines = morning._render_command_center(_dt.date(2026, 7, 6))
    assert lines
    flat = "\n".join(lines)
    assert "COMMAND CENTER" in flat
    # emergency approval surfaced
    assert "EMERGENCY" in flat or "Awaiting approval" in flat
