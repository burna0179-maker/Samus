#!/usr/bin/env python3
"""
GovernanceParliament — hardened multi-agent voting layer
Source: ChatGPT recovery chat 40

Canonical relationship:
- [EXPANDS §8 Stage 5] multi-analyst review (canonical's `threat_assessor` /
  `compliance_officer` / `operational_risk` panel) generalized to N-agent parliament
- [PAIRS WITH] three_plane_authority_model.md (Parliament lives in PLANE 1)
- [PAIRS WITH] autonomy_tier_model.md (LEVEL 5 requires parliament approval)
- [GATES] Stage 7 apply when risk >= 6.0 per canonical §8 routing matrix

Architectural position:
   Planner → GovernanceParliament → Execution Controller

High-risk actions (filesystem writes, network access, plugin installs, model updates,
mutation_apply, capability_register, governance_modify) pass through voting BEFORE execution.

Key hardening over naive boolean voting:
  - Capability-based eligibility (authority_scope)
  - Weighted voting (confidence × reputation)
  - 3-state Vote enum (APPROVE / REJECT / ABSTAIN)
  - Quorum enforcement
  - Risk-adjusted threshold (higher risk → higher bar)
  - Adaptive reputation (post-vote feedback based on outcome alignment)
  - Structured audit trail
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class Vote(Enum):
    APPROVE = 1
    REJECT = -1
    ABSTAIN = 0


@dataclass
class ParliamentAgent:
    """
    Examples of authority_scope:
      ["mutation_apply", "code_modify"]           — code-mutation specialist
      ["governance_modify", "rule_amend"]         — policy specialist
      ["external_egress", "network_action"]       — security specialist
      ["resource_allocation", "compute_budget"]   — economic specialist
    """
    name: str
    authority_scope: List[str]
    confidence: float = 1.0          # 0..1; per-action confidence
    reputation: float = 1.0          # 0..2; tracks alignment with outcomes
    vote_history: List[Dict] = field(default_factory=list)

    def can_vote(self, action: str) -> bool:
        return action in self.authority_scope

    def weight(self) -> float:
        return max(0.0, self.confidence * self.reputation)

    def decide(self, action: str, risk_score: float) -> Vote:
        """
        Default heuristic. Override with LLM / reasoning engine for production.
        For LEVEL 5 mutations, replace with multi-stage reasoning per §8 Stage 5.
        """
        if not self.can_vote(action):
            return Vote.ABSTAIN
        if self.confidence > risk_score:
            return Vote.APPROVE
        elif self.confidence < risk_score * 0.5:
            return Vote.REJECT
        return Vote.ABSTAIN


class GovernanceParliament:
    """
    Constants:
      quorum         — minimum fraction of total agents that must be eligible
      base_approval  — baseline weighted-approval threshold (0.67 default)
      risk_curve     — risk_score adds up to 0.25 to threshold (cap 0.95)
    """

    def __init__(
        self,
        agents: List[ParliamentAgent],
        quorum: float = 0.5,
        base_approval: float = 0.67,
        reputation_gain: float = 0.02,
        reputation_loss: float = 0.05,
    ):
        self.agents = agents
        self.quorum = quorum
        self.base_approval = base_approval
        self.reputation_gain = reputation_gain
        self.reputation_loss = reputation_loss
        self.vote_log: List[Dict[str, Any]] = []

    def _risk_adjusted_threshold(self, risk_score: float) -> float:
        """Higher risk → higher approval bar. Cap at 0.95."""
        return min(0.95, self.base_approval + (risk_score * 0.25))

    def vote(self, action: str, risk_score: float) -> bool:
        eligible = [a for a in self.agents if a.can_vote(action)]
        now = time.time()

        if not eligible:
            self.vote_log.append({
                "action": action, "result": False,
                "reason": "no_eligible_agents", "timestamp": now,
            })
            return False

        quorum_required = max(1, int(len(self.agents) * self.quorum))
        if len(eligible) < quorum_required:
            self.vote_log.append({
                "action": action, "result": False,
                "reason": "quorum_not_met",
                "eligible": len(eligible), "required": quorum_required,
                "timestamp": now,
            })
            return False

        votes = []
        weighted_yes = 0.0
        total_weight = 0.0
        for agent in eligible:
            decision = agent.decide(action, risk_score)
            weight = agent.weight()
            if decision == Vote.APPROVE:
                weighted_yes += weight
            elif decision == Vote.REJECT:
                weighted_yes -= weight
            total_weight += weight
            votes.append({"agent": agent.name, "vote": decision.name, "weight": weight})

        approval_score = (weighted_yes / total_weight) if total_weight else 0.0
        threshold = self._risk_adjusted_threshold(risk_score)
        approved = approval_score >= threshold

        record = {
            "action": action, "risk_score": risk_score,
            "approval_score": approval_score, "threshold": threshold,
            "result": approved, "votes": votes, "timestamp": now,
        }
        self.vote_log.append(record)
        self._update_reputation(votes, approved)
        return approved

    def _update_reputation(self, votes: List[Dict], result: bool) -> None:
        """Reward agents who aligned with the final outcome; penalize the rest."""
        for vote in votes:
            if vote["vote"] == "ABSTAIN":
                continue
            agent = next(a for a in self.agents if a.name == vote["agent"])
            aligned = ((vote["vote"] == "APPROVE" and result) or
                       (vote["vote"] == "REJECT" and not result))
            if aligned:
                agent.reputation = min(2.0, agent.reputation + self.reputation_gain)
            else:
                agent.reputation = max(0.1, agent.reputation - self.reputation_loss)


# Future-extension menu (chat 40):
#   - Byzantine fault tolerance voting (handles adversarial agents)
#   - Multi-chamber governance (technical / safety / economic — bicameral or more)
#   - Mathematical trust scoring (PageRank-style influence)
#   - Constitution/policy constraints (hard veto on certain action classes)
#   - Emergency veto agents (single-agent veto for catastrophic-risk actions)
#   - Time-delayed execution queues (cooling-off period between vote and apply)
