"""Optimizer service orchestrators — bandit + portfolio."""
from __future__ import annotations

import logging
import os
from typing import Any

from backend.common import events, persistence
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from . import bandit, portfolio
from .models import (
    ArmStats,
    BanditState,
    Opportunity,
    OptimizePortfolioRequest,
    OptimizePortfolioResult,
    PortfolioAction,
    RewardWeights,
    SelectArmRequest,
    SelectArmResult,
    UpdateArmRequest,
    UpdateArmResult,
)

_LOG = logging.getLogger("samus.optimizer.service")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/optimizer/optimizer_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(
        os.getenv("SAMUS_OPTIMIZER_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def _state_key(campaign_id: str) -> str:
    return f"optimizer.bandit:{campaign_id}"


def _stats_to_dict(arms: dict[str, ArmStats]) -> dict[str, dict[str, float]]:
    return {k: {"wins": v.wins, "trials": v.trials} for k, v in arms.items()}


def _dict_to_stats(stats: dict[str, dict[str, float]]) -> dict[str, ArmStats]:
    return {
        k: ArmStats(wins=float(v.get("wins", 0.0)), trials=int(v.get("trials", 0)))
        for k, v in stats.items()
    }


def _load_state(campaign_id: str, weights: RewardWeights) -> BanditState:
    raw = GLOBAL_IDEMPOTENCY_STORE.get(_state_key(campaign_id))
    if isinstance(raw, dict):
        return BanditState.model_validate(raw)
    return BanditState(campaign_id=campaign_id, arms={}, total_trials=0, weights=weights)


def _persist(state: BanditState) -> None:
    GLOBAL_IDEMPOTENCY_STORE.set(_state_key(state.campaign_id), state.model_dump())


def select_arm(req: SelectArmRequest) -> SelectArmResult:
    """Pick the next arm via UCB1, seeding state for any new arm in ``req.arms``."""
    weights = req.weights or RewardWeights()
    state = _load_state(req.campaign_id, weights)

    # Seed any new arms with zeroed stats so the loop can pick them.
    for arm in req.arms:
        state.arms.setdefault(arm, ArmStats())

    selected = bandit.ucb1_select(
        _stats_to_dict(state.arms),
        state.total_trials,
        exploration_bias=req.exploration_bias,
    )

    result = SelectArmResult(
        campaign_id=req.campaign_id,
        selected_arm=selected,
        snapshot=state.arms,
        total_trials=state.total_trials,
    )
    _persist(state)

    ev = events.build_audit_event(
        service="optimizer",
        task_id=req.campaign_id,
        action="select_arm",
        input_payload=req.model_dump(),
        output_payload={"selected_arm": selected},
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("optimizer audit append failed: %s", exc)
    return result


def update_arm(req: UpdateArmRequest) -> UpdateArmResult:
    """Record one trial's reward for ``req.arm`` and return updated snapshot."""
    weights = req.weights or RewardWeights()
    state = _load_state(req.campaign_id, weights)

    new_stats_dict, new_total = bandit.apply_reward(
        _stats_to_dict(state.arms), req.arm, req.signals, weights,
    )
    state.arms = _dict_to_stats(new_stats_dict)
    state.total_trials = new_total
    state.weights = weights

    best = bandit.best_arm(new_stats_dict)
    result = UpdateArmResult(
        campaign_id=req.campaign_id,
        arm=req.arm,
        snapshot=state.arms,
        total_trials=state.total_trials,
        best_arm=best,
    )
    _persist(state)

    ev = events.build_audit_event(
        service="optimizer",
        task_id=req.campaign_id,
        action="update_arm",
        input_payload=req.model_dump(),
        output_payload={"arm": req.arm, "total_trials": new_total, "best_arm": best},
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("optimizer audit append failed: %s", exc)
    return result


def optimize_portfolio(req: OptimizePortfolioRequest) -> OptimizePortfolioResult:
    """Rank opportunities + emit per-opportunity actions under a budget cap."""
    # Apply momentum-from-signals if the caller hasn't set momentum directly.
    enriched: list[Opportunity] = []
    for opp in req.opportunities:
        m = opp.momentum
        if m == 0.0 and opp.signals:
            m = portfolio.momentum_from_signals(opp.signals)
        enriched.append(opp.model_copy(update={"momentum": m}))

    scored = sorted(
        ((opp, portfolio.score_opportunity(opp)) for opp in enriched),
        key=lambda x: x[1],
        reverse=True,
    )

    actions: list[PortfolioAction] = []
    total_cost = 0.0
    remaining = float(req.total_budget)
    for rank, (opp, score) in enumerate(scored):
        decision = portfolio.classify_action(rank, score, req.top_tier, req.mid_tier)
        # Charge the budget for accelerate / maintain only.
        projected = 0.0
        if decision in ("accelerate", "maintain"):
            projected = float(opp.execution_cost)
            if total_cost + projected <= req.total_budget:
                total_cost += projected
                remaining -= projected
            else:
                # Budget exhausted — demote to defer.
                decision = "defer"
                projected = 0.0
        actions.append(PortfolioAction(
            prospect_id=opp.prospect_id,
            decision=decision,
            score=score,
            projected_cost=projected,
        ))

    result = OptimizePortfolioResult(
        campaign_id=req.campaign_id,
        actions=actions,
        total_estimated_cost=total_cost,
        budget_remaining=max(0.0, remaining),
    )

    ev = events.build_audit_event(
        service="optimizer",
        task_id=req.campaign_id,
        action="optimize_portfolio",
        input_payload=req.model_dump(),
        output_payload={"action_count": len(actions), "total_cost": total_cost},
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("optimizer audit append failed: %s", exc)
    return result
