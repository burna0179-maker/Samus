#!/usr/bin/env python3
"""
LLMPortfolioManager — hedge-fund-style decision layer
Source: ChatGPT recovery chat 02 (LLM portfolio manager + multi-agent split)

Canonical relationship:
- [EXPANDS §6 model_extended plane] LLM reasoning layer above rule-based optimizer
- [EXPANDS §6 agents plane] introduces ROLE-based pods (PortfolioManager / DealAnalyst / ExecutionOptimizer)
- [DEFERRED] structured tool-calling, JSON-schema-enforced output, prompt-cache integration
- [DEFERRED] cost-budgeted LLM calls (per Anthropic prompt-cache 5min TTL)

3-layer stack (canonical analog):
  Layer 1: LLMPortfolioManager  — THINKS (this file)
  Layer 2: CampaignBandit        — LEARNS (companion: campaign_bandit.py)
  Layer 3: CampaignOptimizer     — ACTS (companion: campaign_optimizer.py)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


PORTFOLIO_MANAGER_PROMPT = """\
You are a hedge fund portfolio manager optimizing a pipeline of deals.

OBJECTIVE:
Maximize expected revenue while minimizing wasted execution cost.

CONSTRAINTS:
- Budget is limited
- Execution is expensive
- Time decay reduces conversion probability

CURRENT STATE:
{state_json}

INSTRUCTIONS:
1. Identify top opportunities
2. Identify underperforming assets
3. Recommend:
   - capital allocation (which prospects to prioritize)
   - actions (fulfillment, outreach, close)
   - any strategy shifts

Return strict JSON, no prose:
{{
  "priorities": [<prospect_id>...],
  "deprioritize": [<prospect_id>...],
  "actions": [
    {{"type": "accelerate"|"replan"|"drop", "prospect_id": "<id>"}}
  ]
}}
"""

DEAL_ANALYST_PROMPT = """\
You are a deal analyst. Given prospect data, return:
  - close_probability (0..1)
  - critical_risks (list of strings)
  - recommended_angle (pain|opportunity|competitive)

Prospect: {prospect_json}
"""

EXECUTION_OPTIMIZER_PROMPT = """\
You are an execution optimizer. Given an action plan and recent bandit stats,
return the bandit-arm choice per action.

Plan: {plan_json}
Bandit stats: {bandit_json}
"""


@dataclass
class PortfolioState:
    budget_remaining: float
    avg_conversion_rate: float
    pipeline_value: float
    active_prospects: int
    prospects: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMDecision:
    priorities: List[str] = field(default_factory=list)
    deprioritize: List[str] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)


class LLMPortfolioManager:
    """LLM-driven portfolio decisioning. Pluggable LLM callable."""

    def __init__(self, llm_call: Callable[[str], str]):
        self._llm = llm_call

    def decide(self, state: PortfolioState) -> LLMDecision:
        prompt = PORTFOLIO_MANAGER_PROMPT.format(state_json=json.dumps(state.__dict__, default=str))
        raw = self._llm(prompt)
        return self._parse(raw)

    def _parse(self, raw: str) -> LLMDecision:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # Fail-closed: empty decision rather than crash
            return LLMDecision()
        return LLMDecision(
            priorities=obj.get("priorities", []),
            deprioritize=obj.get("deprioritize", []),
            actions=obj.get("actions", []),
        )


class DealAnalyst:
    def __init__(self, llm_call: Callable[[str], str]):
        self._llm = llm_call

    def analyze(self, prospect: Dict[str, Any]) -> Dict[str, Any]:
        prompt = DEAL_ANALYST_PROMPT.format(prospect_json=json.dumps(prospect, default=str))
        raw = self._llm(prompt)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"close_probability": 0.0, "critical_risks": ["llm_parse_failure"], "recommended_angle": "opportunity"}


class ExecutionOptimizer:
    def __init__(self, llm_call: Callable[[str], str], bandit: Optional[Any] = None):
        self._llm = llm_call
        self._bandit = bandit

    def select(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        bandit_stats = self._bandit.stats if self._bandit else {}
        prompt = EXECUTION_OPTIMIZER_PROMPT.format(
            plan_json=json.dumps(plan, default=str),
            bandit_json=json.dumps(bandit_stats, default=str),
        )
        try:
            return json.loads(self._llm(prompt))
        except json.JSONDecodeError:
            return {"arm_choice": "default"}


def execute_llm_decision(decision: LLMDecision, dispatcher: Callable[..., None]) -> None:
    for a in decision.actions:
        t = a.get("type")
        pid = a.get("prospect_id")
        if t == "accelerate":
            dispatcher(service="fulfillment", action="resume_plan", payload={"prospect_id": pid})
        elif t == "replan":
            dispatcher(service="fulfillment", action="plan_execution", payload={"prospect_id": pid})
        elif t == "drop":
            dispatcher(service="crm", action="mark_cold", payload={"prospect_id": pid})
