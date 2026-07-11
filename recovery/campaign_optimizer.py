#!/usr/bin/env python3
"""
CampaignOptimizer — portfolio-level resource allocator
Source: ChatGPT recovery chat 02 (campaign optimizer section)

Canonical relationship:
- [NEW] portfolio layer above StrategyEngine; no canonical equivalent
- [EXPANDS §6 autonomy] global decision (vs per-prospect StrategyEngine local decision)

Objective: maximize expected_revenue - execution_cost - time_decay_risk
Allocation tiers (by rank):
  rank < 5   → accelerate (high ROI)
  rank < 15  → maintain
  rank >= 15 → defer
  score <= 0 → deprioritize (drop to cold)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class Opportunity:
    prospect_id: str
    expected_value: float = 1000.0
    conversion_prob: float = 0.1
    time_to_close: int = 7        # days
    execution_cost: float = 100.0
    stage: str = "new"
    momentum: float = 0.0


@dataclass
class OptimizationResult:
    actions: List[Dict[str, Any]] = field(default_factory=list)
    total_estimated_cost: float = 0.0
    budget_remaining: float = 0.0


def score_opportunity(p: Opportunity) -> float:
    momentum_boost = 1 + p.momentum
    time_penalty = 1 / max(p.time_to_close, 1)
    return max((p.expected_value * p.conversion_prob * momentum_boost * time_penalty) - p.execution_cost, 0.0)


def momentum_from_signals(signals: List[str]) -> float:
    score = 0.0
    if "email_open" in signals:
        score += 0.2
    if "link_click" in signals:
        score += 0.3
    if "pricing_request" in signals:
        score += 0.5
    return min(score, 1.0)


class CampaignOptimizer:
    TOP_TIER = 5
    MID_TIER = 15

    def __init__(self, total_budget: float = 5000.0):
        self.total_budget = total_budget

    def optimize(self, portfolio: List[Opportunity]) -> OptimizationResult:
        scored = sorted(((p, score_opportunity(p)) for p in portfolio),
                        key=lambda x: x[1], reverse=True)
        result = OptimizationResult(budget_remaining=self.total_budget)
        for rank, (p, score) in enumerate(scored):
            if score <= 0:
                result.actions.append(self._deprioritize(p))
                continue
            if rank < self.TOP_TIER:
                action = self._accelerate(p)
            elif rank < self.MID_TIER:
                action = self._maintain(p)
            else:
                action = self._defer(p)
            if result.total_estimated_cost + p.execution_cost <= self.total_budget:
                result.actions.append(action)
                result.total_estimated_cost += p.execution_cost
                result.budget_remaining -= p.execution_cost
        return result

    # ----- action factories -----
    def _accelerate(self, p: Opportunity) -> Dict[str, Any]:
        return {"type": "accelerate", "prospect_id": p.prospect_id, "priority": "high",
                "actions": [("fulfillment", "resume_plan"),
                            ("outreach", "send_followup"),
                            ("outreach", "send_close")]}

    def _maintain(self, p: Opportunity) -> Dict[str, Any]:
        return {"type": "maintain", "prospect_id": p.prospect_id, "priority": "normal",
                "actions": [("strategy", "evaluate")]}

    def _defer(self, p: Opportunity) -> Dict[str, Any]:
        return {"type": "defer", "prospect_id": p.prospect_id, "priority": "low",
                "actions": []}

    def _deprioritize(self, p: Opportunity) -> Dict[str, Any]:
        return {"type": "drop", "prospect_id": p.prospect_id,
                "actions": [("crm", "mark_cold")]}


def dispatch_actions(result: OptimizationResult, dispatcher: Callable[..., None]) -> None:
    for action in result.actions:
        for service, act in action.get("actions", []):
            dispatcher(service=service, action=act,
                       payload={"prospect_id": action["prospect_id"]})
