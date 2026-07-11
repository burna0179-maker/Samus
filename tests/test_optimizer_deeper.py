"""Deeper optimizer coverage — bandit, portfolio, service, app endpoints."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.optimizer.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


# ---------------------------------------------------------------------------
# bandit (pure functions)
# ---------------------------------------------------------------------------


def test_ucb1_picks_untried_arm_first():
    from backend.optimizer.bandit import ucb1_select

    stats = {
        "arm_a": {"wins": 5, "trials": 10},
        "arm_b": {"wins": 0, "trials": 0},
        "arm_c": {"wins": 3, "trials": 5},
    }
    assert ucb1_select(stats, total_trials=15) == "arm_b"


def test_ucb1_picks_high_mean_when_all_tried():
    from backend.optimizer.bandit import ucb1_select

    stats = {
        "arm_a": {"wins": 10, "trials": 10},
        "arm_b": {"wins": 2, "trials": 10},
    }
    assert ucb1_select(stats, total_trials=20, exploration_bias=2.0) == "arm_a"


def test_ucb1_exploration_bias_affects_choice():
    """High exploration_bias flips the choice toward the low-trial arm."""
    from backend.optimizer.bandit import ucb1_select

    stats = {
        "arm_a": {"wins": 9, "trials": 10},
        "arm_b": {"wins": 2, "trials": 5},
    }
    assert ucb1_select(stats, total_trials=15, exploration_bias=2.0) == "arm_a"
    assert ucb1_select(stats, total_trials=15, exploration_bias=50.0) == "arm_b"


def test_apply_reward_compound():
    from backend.optimizer.bandit import apply_reward
    from backend.optimizer.models import RewardSignals, RewardWeights

    stats = {"arm_a": {"wins": 0.0, "trials": 0}}
    weights = RewardWeights(reply_weight=1.0, conversion_weight=5.0, engagement_weight=0.5)
    signals = RewardSignals(email_reply=1, closed_deal=1, click_rate=0.5)
    new_stats, total = apply_reward(stats, "arm_a", signals, weights)
    assert abs(new_stats["arm_a"]["wins"] - 6.25) < 1e-9
    assert new_stats["arm_a"]["trials"] == 1
    assert total == 1


def test_apply_reward_creates_new_arm_entry():
    from backend.optimizer.bandit import apply_reward
    from backend.optimizer.models import RewardSignals, RewardWeights

    new_stats, total = apply_reward(
        {},
        "new_arm",
        RewardSignals(email_reply=1, closed_deal=0, click_rate=0.0),
        RewardWeights(),
    )
    assert "new_arm" in new_stats
    assert new_stats["new_arm"]["trials"] == 1
    assert new_stats["new_arm"]["wins"] >= 1
    assert total == 1


def test_best_arm_chooses_highest_mean():
    from backend.optimizer.bandit import best_arm

    stats = {
        "a": {"wins": 8.0, "trials": 10},
        "b": {"wins": 1.0, "trials": 10},
        "c": {"wins": 4.0, "trials": 5},
    }
    assert best_arm(stats) in ("a", "c")
    assert best_arm({}) is None


# ---------------------------------------------------------------------------
# portfolio (pure functions)
# ---------------------------------------------------------------------------


def test_score_opportunity_increases_with_expected_value():
    from backend.optimizer.models import Opportunity
    from backend.optimizer.portfolio import score_opportunity

    lo = Opportunity(prospect_id="lo", expected_value=1_000, conversion_prob=0.3, execution_cost=50)
    hi = Opportunity(prospect_id="hi", expected_value=2_000, conversion_prob=0.3, execution_cost=50)
    assert score_opportunity(hi) > score_opportunity(lo)


def test_score_opportunity_clamped_above_zero():
    from backend.optimizer.models import Opportunity
    from backend.optimizer.portfolio import score_opportunity

    opp = Opportunity(
        prospect_id="boom", expected_value=10.0, conversion_prob=0.01, execution_cost=10_000.0
    )
    assert score_opportunity(opp) == 0.0


def test_momentum_from_signals_cap_at_one():
    from backend.optimizer.portfolio import momentum_from_signals

    assert (
        momentum_from_signals(
            [
                "email_open",
                "link_click",
                "pricing_request",
                "reply",
                "demo_request",
            ]
        )
        == 1.0
    )
    assert momentum_from_signals([]) == 0.0
    assert momentum_from_signals(["totally_unknown_signal"]) == 0.0


def test_classify_action_tier_boundaries():
    from backend.optimizer.portfolio import classify_action

    assert classify_action(0, 10.0, 5, 15) == "accelerate"
    assert classify_action(5, 10.0, 5, 15) == "maintain"
    assert classify_action(14, 10.0, 5, 15) == "maintain"
    assert classify_action(15, 10.0, 5, 15) == "defer"
    assert classify_action(0, 0.0, 5, 15) == "deprioritize"
    assert classify_action(0, -5.0, 5, 15) == "deprioritize"


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def test_select_arm_initializes_state(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import SelectArmRequest
    from backend.optimizer.service import select_arm

    req = SelectArmRequest(campaign_id="c-init", arms=["a", "b", "c"])
    first = select_arm(req)
    assert first.selected_arm in {"a", "b", "c"}
    assert first.total_trials == 0
    second = select_arm(req)
    assert second.selected_arm == first.selected_arm
    assert second.total_trials == 0
    assert set(second.snapshot.keys()) == {"a", "b", "c"}


def test_select_arm_then_update_arm_persists_state(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import (
        RewardSignals,
        SelectArmRequest,
        UpdateArmRequest,
    )
    from backend.optimizer.service import select_arm, update_arm

    select_arm(SelectArmRequest(campaign_id="c-persist", arms=["a", "b"]))
    upd = update_arm(
        UpdateArmRequest(
            campaign_id="c-persist",
            arm="a",
            signals=RewardSignals(email_reply=1, closed_deal=1, click_rate=0.5),
        )
    )
    assert upd.total_trials == 1
    assert upd.snapshot["a"].trials == 1
    assert upd.snapshot["a"].wins > 0
    assert upd.best_arm == "a"

    third = select_arm(SelectArmRequest(campaign_id="c-persist", arms=["a", "b"]))
    assert third.selected_arm == "b"
    assert third.snapshot["a"].trials == 1
    assert third.total_trials == 1


def test_optimize_portfolio_under_budget(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import (
        Opportunity,
        OptimizePortfolioRequest,
    )
    from backend.optimizer.service import optimize_portfolio

    opps = [
        Opportunity(
            prospect_id=f"p{i}",
            expected_value=10_000,
            conversion_prob=0.5,
            execution_cost=100.0,
            time_to_close=1,
        )
        for i in range(3)
    ]
    result = optimize_portfolio(
        OptimizePortfolioRequest(
            campaign_id="under",
            total_budget=1000.0,
            opportunities=opps,
        )
    )
    decisions = {a.prospect_id: a.decision for a in result.actions}
    assert all(d == "accelerate" for d in decisions.values())
    assert result.total_estimated_cost == 300.0
    assert result.budget_remaining == 700.0


def test_optimize_portfolio_over_budget_demotes_to_defer(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import (
        Opportunity,
        OptimizePortfolioRequest,
    )
    from backend.optimizer.service import optimize_portfolio

    opps = [
        Opportunity(
            prospect_id=f"p{i}",
            expected_value=10_000,
            conversion_prob=0.5,
            execution_cost=200.0,
            time_to_close=1,
        )
        for i in range(10)
    ]
    result = optimize_portfolio(
        OptimizePortfolioRequest(
            campaign_id="over",
            total_budget=300.0,
            opportunities=opps,
        )
    )
    accels = [a for a in result.actions if a.decision == "accelerate"]
    defers = [a for a in result.actions if a.decision == "defer"]
    assert len(accels) == 1
    assert len(defers) == 9
    assert result.total_estimated_cost == 200.0


def test_optimize_portfolio_uses_signals_for_momentum_when_zero(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import (
        Opportunity,
        OptimizePortfolioRequest,
    )
    from backend.optimizer.service import optimize_portfolio

    op_with = Opportunity(
        prospect_id="with_sigs",
        expected_value=10_000,
        conversion_prob=0.5,
        execution_cost=10.0,
        time_to_close=1,
        momentum=0.0,
        signals=["email_open", "link_click"],
    )
    op_without = Opportunity(
        prospect_id="no_sigs",
        expected_value=10_000,
        conversion_prob=0.5,
        execution_cost=10.0,
        time_to_close=1,
        momentum=0.0,
    )
    result = optimize_portfolio(
        OptimizePortfolioRequest(
            campaign_id="sig",
            total_budget=10_000.0,
            opportunities=[op_with, op_without],
        )
    )
    by_id = {a.prospect_id: a for a in result.actions}
    assert by_id["with_sigs"].score > by_id["no_sigs"].score


# ---------------------------------------------------------------------------
# app endpoints (TestClient)
# ---------------------------------------------------------------------------


def test_select_arm_endpoint_happy_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.optimizer.app import app

    client = TestClient(app)
    r = client.post(
        "/select_arm",
        json={
            "campaign_id": "c-ep",
            "arms": ["x", "y", "z"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["campaign_id"] == "c-ep"
    assert body["selected_arm"] in {"x", "y", "z"}
    assert body["total_trials"] == 0
    assert set(body["snapshot"].keys()) == {"x", "y", "z"}


def test_update_arm_endpoint_happy_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.optimizer.app import app

    client = TestClient(app)
    client.post(
        "/select_arm",
        json={
            "campaign_id": "c-upd",
            "arms": ["a", "b"],
        },
    )
    r = client.post(
        "/update_arm",
        json={
            "campaign_id": "c-upd",
            "arm": "a",
            "signals": {"email_reply": 1, "closed_deal": 1, "click_rate": 0.0},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["arm"] == "a"
    assert body["best_arm"] == "a"
    assert body["total_trials"] == 1
    assert body["snapshot"]["a"]["trials"] == 1


def test_optimize_portfolio_endpoint_happy_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.optimizer.app import app

    client = TestClient(app)
    r = client.post(
        "/optimize_portfolio",
        json={
            "campaign_id": "c-opt",
            "total_budget": 5_000.0,
            "opportunities": [
                {
                    "prospect_id": f"p{i}",
                    "expected_value": 10_000,
                    "conversion_prob": 0.5,
                    "execution_cost": 100.0,
                    "time_to_close": 1,
                }
                for i in range(5)
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["actions"]) == 5
    assert body["budget_remaining"] >= 0.0


def test_capability_denied_on_unknown_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.optimizer.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-bogus",
            "payload": {"campaign_id": "x", "arms": ["a"]},
            "metadata": {"action": "bogus"},
        },
    )
    assert r.status_code == 400
    assert "unknown_action" in r.text
