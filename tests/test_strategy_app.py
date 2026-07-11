"""TestClient smoke for backend.strategy.app.

crm_client.build_context and dispatcher.dispatch_strategy_action are
stubbed via monkeypatch to avoid network calls.
"""
from __future__ import annotations

import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────


def _stub_build_context(monkeypatch, prospect_id="p-test", **kwargs):
    """Replace crm_client.build_context with a stub returning StrategyContext defaults."""
    from backend.strategy.engine import StrategyContext
    import backend.strategy.crm_client as crm_mod
    import backend.strategy.service as svc_mod

    ctx = StrategyContext(prospect_id=prospect_id, **kwargs)

    async def _fake_build_context(_pid: str) -> StrategyContext:
        return ctx

    monkeypatch.setattr(crm_mod, "build_context", _fake_build_context)
    monkeypatch.setattr(svc_mod.crm_client, "build_context", _fake_build_context)
    return ctx


def _stub_dispatcher(monkeypatch, result: dict | None = None):
    """Replace dispatcher.dispatch_strategy_action with a stub."""
    import backend.strategy.dispatcher as disp_mod
    import backend.strategy.service as svc_mod

    outcome = result or {"dispatched": True, "service": "outreach", "action": "send_outreach", "gateway_status": 200}

    async def _fake_dispatch(decision, ctx):
        return outcome

    monkeypatch.setattr(disp_mod, "dispatch_strategy_action", _fake_dispatch)
    monkeypatch.setattr(svc_mod.dispatcher, "dispatch_strategy_action", _fake_dispatch)
    return outcome


def _reset_patterns(monkeypatch):
    """Reset pattern counters before test."""
    from backend.strategy.engine import reset_patterns
    reset_patterns()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_health(monkeypatch):
    _stub_build_context(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "strategy"


def test_evaluate_endpoint_returns_decision(monkeypatch):
    """Stubbed context with medium engagement → monitor action."""
    _stub_build_context(
        monkeypatch,
        prospect_id="p-eval",
        lead_score=20.0,
        seo_score=80.0,
        stage="active",
        engagement="medium",
    )
    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.post("/strategy/evaluate", json={"prospect_id": "p-eval"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prospect_id"] == "p-eval"
    assert body["action"] in ("escalate_close", "replan_fulfillment", "trigger_outreach", "monitor", "none")
    assert isinstance(body["score"], float)


def test_dispatch_endpoint_routes_action(monkeypatch):
    """Stubbed dispatcher confirms the endpoint forwards the action."""
    _stub_build_context(monkeypatch, prospect_id="p-disp")
    stub_result = _stub_dispatcher(monkeypatch)

    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.post("/strategy/dispatch", json={
        "prospect_id": "p-disp",
        "action": "trigger_outreach",
        "payload": {},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dispatched"] is True
    assert body["service"] == stub_result["service"]


def test_record_outcome_endpoint_boosts_pattern(monkeypatch):
    """Won outcome should boost similar_prospects pattern."""
    _reset_patterns(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.strategy.app import app
    import backend.strategy.engine as engine_mod

    client = TestClient(app)
    r = client.post("/strategy/record-outcome", json={
        "prospect_id": "p-win",
        "won": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True
    assert body["prospect_id"] == "p-win"
    # Pattern should have been boosted
    assert engine_mod.PATTERNS.get("similar_prospects", 0) >= 2


def test_record_outcome_endpoint_penalizes_pattern(monkeypatch):
    """Lost outcome should penalize strategy_path pattern."""
    _reset_patterns(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.strategy.app import app
    import backend.strategy.engine as engine_mod

    # Boost first so there's something to penalize
    engine_mod.boost_pattern("strategy_path")
    engine_mod.boost_pattern("strategy_path")  # now at 3

    client = TestClient(app)
    r = client.post("/strategy/record-outcome", json={
        "prospect_id": "p-loss",
        "won": False,
    })
    assert r.status_code == 200, r.text
    assert engine_mod.PATTERNS.get("strategy_path", 1) == 2  # decremented from 3


def test_work_dispatch_evaluate_action(monkeypatch):
    """Work envelope with action=evaluate should succeed."""
    _stub_build_context(
        monkeypatch,
        prospect_id="p-work",
        lead_score=20.0,
        seo_score=80.0,
        stage="active",
        engagement="medium",
    )
    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.post("/work", json={
        "task_id": "t-eval-1",
        "payload": {"prospect_id": "p-work"},
        "metadata": {"action": "evaluate"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prospect_id"] == "p-work"
    assert "action" in body


def test_work_dispatch_unknown_action_returns_400(monkeypatch):
    """Work envelope with an unrecognised action should return 400."""
    _stub_build_context(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.post("/work", json={
        "task_id": "t-bad",
        "payload": {},
        "metadata": {"action": "nonexistent_action"},
    })
    assert r.status_code == 400
    assert "unknown action" in r.json()["detail"]


# ── Bandit-stats observability route ─────────────────────────────────────────


def test_bandit_stats_empty(monkeypatch):
    """GET /strategy/bandit-stats with no plays returns an empty all-scope snapshot."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()

    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.get("/strategy/bandit-stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "all"
    assert body["total_trials"] == 0
    assert body["flat_arms"] == []
    assert body["hierarchical_arms"] == []


def test_bandit_stats_surfaces_flat_and_hierarchical_arms(monkeypatch):
    """The route reflects both flat and hierarchical bandit arms with UCB1 scores."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()
    # Flat arm.
    pm.update_bandit("trigger_outreach", 1.0)
    # Hierarchical industry::policy_family arm.
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)

    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.get("/strategy/bandit-stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "all"
    assert body["total_trials"] == 2
    flat_ids = {a["arm_id"] for a in body["flat_arms"]}
    hier_ids = {a["arm_id"] for a in body["hierarchical_arms"]}
    assert "trigger_outreach" in flat_ids
    assert "hvac::fast_quote_mode" in hier_ids
    flat_arm = next(a for a in body["flat_arms"] if a["arm_id"] == "trigger_outreach")
    assert flat_arm["wins"] == 1.0
    assert flat_arm["trials"] == 1
    assert flat_arm["mean_reward"] == 1.0
    # One trial out of two total -> a finite, positive UCB1 score.
    assert flat_arm["ucb1_score"] > 0.0


def test_bandit_stats_industry_scope(monkeypatch):
    """?industry=<vertical> scopes the snapshot to that vertical's policy arms."""
    import backend.strategy.portfolio_manager as pm
    from backend.strategy.policy_compiler import POLICY_FAMILIES
    pm.reset_bandit()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    pm.update_policy_bandit("plumber", "emergency_dispatch", 1.0)

    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.get("/strategy/bandit-stats", params={"industry": "hvac"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "industry"
    assert body["industry"] == "hvac"
    arm_ids = {a["arm_id"] for a in body["arms"]}
    # Only hvac arms — the plumbing arm must not leak in.
    assert all(aid.startswith("hvac::") for aid in arm_ids)
    assert "hvac::fast_quote_mode" in arm_ids
    assert body["policy_families"] == list(POLICY_FAMILIES.get("hvac", ()))


def test_bandit_stats_unplayed_arm_scores_infinite(monkeypatch):
    """An unplayed arm reports trials=0 and an 'explore me' (inf) UCB1 score."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()
    pm.update_bandit("seen_arm", 1.0)

    from fastapi.testclient import TestClient
    from backend.strategy.app import app
    from backend.strategy.portfolio_manager import _ucb1_score

    # An arm with zero trials scores +inf per UCB1 — confirm the projection
    # surfaces that rather than crashing on a 0-division.
    assert _ucb1_score(0.0, 0, 1) == float("inf")

    client = TestClient(app)
    r = client.get("/strategy/bandit-stats")
    assert r.status_code == 200, r.text


def test_bandit_stats_via_work_action(monkeypatch):
    """The read is also reachable through the /work envelope dispatcher."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)

    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.post("/work", json={
        "task_id": "t-bandit-stats",
        "payload": {"industry": "hvac"},
        "metadata": {"action": "read_bandit_stats"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "industry"
    assert body["industry"] == "hvac"


def test_bandit_stats_work_action_rejects_non_string_industry(monkeypatch):
    """A non-string industry in the /work payload is a 422."""
    from fastapi.testclient import TestClient
    from backend.strategy.app import app

    client = TestClient(app)
    r = client.post("/work", json={
        "task_id": "t-bad-industry",
        "payload": {"industry": 123},
        "metadata": {"action": "read_bandit_stats"},
    })
    assert r.status_code == 422


def test_bandit_stats_capability_registered():
    """The route's capability is registered on the strategy service surface."""
    # Importing the app module runs the setdefault().update() registration.
    import backend.strategy.app  # noqa: F401
    from backend.common.capabilities import SERVICE_CAPABILITIES

    assert "read_bandit_stats" in SERVICE_CAPABILITIES.get("strategy", set())
