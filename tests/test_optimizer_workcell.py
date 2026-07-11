"""Smoke tests for the optimizer workcell — UCB1 bandit + portfolio scorer."""
from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.optimizer.service as svc_mod
    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_bandit_untried_arm_picked_first():
    from backend.optimizer.bandit import ucb1_select
    stats = {"a": {"wins": 0, "trials": 0}, "b": {"wins": 5, "trials": 3}}
    # Untried arm 'a' wins
    assert ucb1_select(stats, total_trials=3) == "a"


def test_bandit_picks_best_when_all_tried():
    from backend.optimizer.bandit import ucb1_select
    stats = {
        "a": {"wins": 9, "trials": 10},  # mean 0.9
        "b": {"wins": 1, "trials": 10},  # mean 0.1
    }
    assert ucb1_select(stats, total_trials=20) == "a"


def test_bandit_apply_reward():
    from backend.optimizer.bandit import apply_reward
    from backend.optimizer.models import RewardSignals, RewardWeights
    stats = {"a": {"wins": 0, "trials": 0}}
    new_stats, total = apply_reward(
        stats, "a",
        RewardSignals(email_reply=1, closed_deal=1, click_rate=0.2),
        RewardWeights(),
    )
    # reward = 1*1 + 5*1 + 0.5*0.2 = 6.1
    assert abs(new_stats["a"]["wins"] - 6.1) < 1e-6
    assert new_stats["a"]["trials"] == 1
    assert total == 1


def test_portfolio_momentum_capped():
    from backend.optimizer.portfolio import momentum_from_signals
    # Sum > 1.0 → capped at 1.0
    assert momentum_from_signals(
        ["email_open", "link_click", "pricing_request", "reply", "demo_request"],
    ) == 1.0
    assert momentum_from_signals([]) == 0.0


def test_portfolio_score_monotonicity():
    from backend.optimizer.models import Opportunity
    from backend.optimizer.portfolio import score_opportunity
    # Both clear the cost floor so the comparison is meaningful.
    lo = Opportunity(prospect_id="lo", expected_value=10_000,
                     conversion_prob=0.1, execution_cost=50)
    hi = Opportunity(prospect_id="hi", expected_value=10_000,
                     conversion_prob=0.5, execution_cost=50)
    assert score_opportunity(hi) > score_opportunity(lo)
    assert score_opportunity(lo) > 0


def test_portfolio_classify_tiers():
    from backend.optimizer.portfolio import classify_action
    assert classify_action(0, 100, 5, 15) == "accelerate"
    assert classify_action(4, 100, 5, 15) == "accelerate"
    assert classify_action(5, 100, 5, 15) == "maintain"
    assert classify_action(14, 100, 5, 15) == "maintain"
    assert classify_action(15, 100, 5, 15) == "defer"
    assert classify_action(0, 0, 5, 15) == "deprioritize"


def test_service_select_then_update(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import (
        RewardSignals, SelectArmRequest, UpdateArmRequest,
    )
    from backend.optimizer.service import select_arm, update_arm

    pick = select_arm(SelectArmRequest(
        campaign_id="c1", arms=["a", "b", "c"],
    ))
    assert pick.selected_arm in {"a", "b", "c"}
    assert pick.total_trials == 0

    upd = update_arm(UpdateArmRequest(
        campaign_id="c1", arm=pick.selected_arm,
        signals=RewardSignals(closed_deal=1),
    ))
    assert upd.total_trials == 1
    assert upd.snapshot[pick.selected_arm].trials == 1


def test_service_optimize_portfolio(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OPTIMIZER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.optimizer.models import (
        Opportunity, OptimizePortfolioRequest,
    )
    from backend.optimizer.service import optimize_portfolio

    opps = [
        Opportunity(prospect_id="p1", expected_value=10_000, conversion_prob=0.5),
        Opportunity(prospect_id="p2", expected_value=1_000, conversion_prob=0.1),
        Opportunity(prospect_id="p3", expected_value=100, conversion_prob=0.01),
    ]
    result = optimize_portfolio(OptimizePortfolioRequest(
        campaign_id="c1", total_budget=500.0, opportunities=opps,
    ))
    assert len(result.actions) == 3
    # Highest-EV opportunity is accelerated first.
    by_id = {a.prospect_id: a for a in result.actions}
    assert by_id["p1"].decision == "accelerate"
    # Budget caps total spend
    assert result.total_estimated_cost <= 500.0
