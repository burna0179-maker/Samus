"""Quorum voter — Stage 5 multi-analyst review for mutation pipeline.

Implements the §8 Stage 5 approve gate as a hardened weighted-vote primitive.
For LEVEL 5 autonomy mutations, GovernanceParliament invokes this voter with
the {threat_assessor, compliance_officer, operational_risk} analyst panel.

Capability-scoped, weighted (confidence × reputation), risk-adjusted threshold,
reputation feedback after each vote.

Target path: backend/standard/agents/parliament/quorum_voter.py
Source recovery: governance_parliament.py (chat 40)

v3 rename: file is `quorum_voter.py` (not `parliament_runtime.py`); the
directory name `parliament/` is retained as a known industry metaphor that
still describes the multi-voter pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.core.configuration.settings import get_settings
from backend.core.mutation.scope import mutation_scope, MutationType
from backend.core.protocols import HealthReport, HealthStatus

__plane__ = "agents"


class Vote(Enum):
    APPROVE = 1
    REJECT = -1
    ABSTAIN = 0


@dataclass
class VoterAgent:
    """Analyst with a bounded authority_scope. Examples:
      ["mutation_apply"]         — code-mutation specialist
      ["governance_modify"]      — policy specialist
      ["external_egress"]        — security specialist
    """
    name: str
    authority_scope: tuple[str, ...]
    confidence: float = 1.0          # 0..1; per-action confidence
    reputation: float = 1.0          # 0..2; tracks alignment with outcomes

    def can_vote(self, action: str) -> bool:
        return action in self.authority_scope

    def weight(self) -> float:
        return max(0.0, self.confidence * self.reputation)

    def decide(self, action: str, risk_score: float) -> Vote:
        """Default heuristic. Override with LLM/reasoning engine for production.

        For LEVEL 5 mutations, replace with multi-stage reasoning per canonical
        §8 Stage 5 (threat_assessor / compliance_officer / operational_risk
        each producing a 5-section verdict).
        """
        if not self.can_vote(action):
            return Vote.ABSTAIN
        if self.confidence > risk_score:
            return Vote.APPROVE
        if self.confidence < risk_score * 0.5:
            return Vote.REJECT
        return Vote.ABSTAIN


@dataclass(frozen=True)
class QuorumDecision:
    approved: bool
    approval_score: float
    threshold: float
    votes: tuple[dict, ...]
    reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "approval_score": self.approval_score,
            "threshold": self.threshold,
            "votes": list(self.votes),
            "reason": self.reason,
            "ts": self.ts,
        }


@mutation_scope(
    state_paths=("data/parliament/**",),
    mutation_types={MutationType.STATE},
)
class QuorumVoter:
    """Capability-scoped weighted-vote engine.

    Architectural position:
        Planner → QuorumVoter → Execution Controller

    High-risk actions (mutation_apply, capability_register, governance_modify,
    external_egress) pass through voting BEFORE Stage 7 apply.

    Settings driven:
        sn_parliament_quorum            — min fraction eligible
        sn_parliament_base_approval     — baseline weighted threshold
        sn_parliament_reputation_gain   — per-aligned-vote reward
        sn_parliament_reputation_loss   — per-misaligned-vote penalty
    """

    plane_name = "agents:parliament:quorum_voter"

    def __init__(self, voters: list[VoterAgent]) -> None:
        cfg = get_settings()
        self.voters = voters
        self._quorum = getattr(cfg, "sn_parliament_quorum", 0.5)
        self._base_approval = getattr(cfg, "sn_parliament_base_approval", 0.67)
        self._reputation_gain = getattr(cfg, "sn_parliament_reputation_gain", 0.02)
        self._reputation_loss = getattr(cfg, "sn_parliament_reputation_loss", 0.05)
        self._vote_log: list[QuorumDecision] = []

    async def vote(self, action: str, risk_score: float) -> QuorumDecision:
        """Async to match canonical Protocol convention. Pure-compute internally."""
        eligible = [v for v in self.voters if v.can_vote(action)]
        now = time.time()

        if not eligible:
            decision = QuorumDecision(
                approved=False, approval_score=0.0, threshold=0.0,
                votes=(), reason="no_eligible_voters", ts=now,
            )
            self._vote_log.append(decision)
            return decision

        quorum_required = max(1, int(len(self.voters) * self._quorum))
        if len(eligible) < quorum_required:
            decision = QuorumDecision(
                approved=False, approval_score=0.0, threshold=0.0,
                votes=(),
                reason=f"quorum_not_met: {len(eligible)}/{quorum_required}",
                ts=now,
            )
            self._vote_log.append(decision)
            return decision

        votes: list[dict] = []
        weighted_yes = 0.0
        total_weight = 0.0
        for voter in eligible:
            v = voter.decide(action, risk_score)
            w = voter.weight()
            if v == Vote.APPROVE:
                weighted_yes += w
            elif v == Vote.REJECT:
                weighted_yes -= w
            total_weight += w
            votes.append({"voter": voter.name, "vote": v.name, "weight": w})

        approval_score = (weighted_yes / total_weight) if total_weight else 0.0
        threshold = self._risk_adjusted_threshold(risk_score)
        approved = approval_score >= threshold

        decision = QuorumDecision(
            approved=approved, approval_score=approval_score,
            threshold=threshold, votes=tuple(votes), ts=now,
        )
        self._vote_log.append(decision)
        self._update_reputation(votes, approved)
        return decision

    def _risk_adjusted_threshold(self, risk_score: float) -> float:
        """Higher risk → higher approval bar. Cap at 0.95."""
        return min(0.95, self._base_approval + (risk_score * 0.25))

    def _update_reputation(self, votes: list[dict], result: bool) -> None:
        for vote in votes:
            if vote["vote"] == "ABSTAIN":
                continue
            voter = next(v for v in self.voters if v.name == vote["voter"])
            aligned = (
                (vote["vote"] == "APPROVE" and result)
                or (vote["vote"] == "REJECT" and not result)
            )
            if aligned:
                voter.reputation = min(2.0, voter.reputation + self._reputation_gain)
            else:
                voter.reputation = max(0.1, voter.reputation - self._reputation_loss)

    def vote_log(self, limit: int = 100) -> list[dict]:
        return [d.to_dict() for d in self._vote_log[-limit:]]

    def health(self) -> HealthReport:
        if not self.voters:
            return HealthReport(
                status=HealthStatus.CRITICAL,
                detail="no voters registered",
            )
        avg_rep = sum(v.reputation for v in self.voters) / len(self.voters)
        return HealthReport(
            status=HealthStatus.OK if avg_rep > 0.5 else HealthStatus.DEGRADED,
            detail=f"{len(self.voters)} voters, avg_reputation={avg_rep:.2f}",
            metrics={
                "voter_count": float(len(self.voters)),
                "avg_reputation": avg_rep,
                "vote_history_size": float(len(self._vote_log)),
            },
        )


_instance: QuorumVoter | None = None


def get_quorum_voter() -> QuorumVoter:
    """Module-level singleton accessor — matches canonical pattern."""
    global _instance
    if _instance is None:
        # Default analyst panel per canonical §8 Stage 5
        _instance = QuorumVoter(voters=[
            VoterAgent(
                name="threat_assessor",
                authority_scope=("mutation_apply", "external_egress", "capability_register"),
            ),
            VoterAgent(
                name="compliance_officer",
                authority_scope=("mutation_apply", "governance_modify", "rule_amend"),
            ),
            VoterAgent(
                name="operational_risk",
                authority_scope=("mutation_apply", "resource_allocation"),
            ),
        ])
    return _instance
