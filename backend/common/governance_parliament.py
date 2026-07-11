"""Multi-agent voting advisory layer for borderline governance decisions.

Ported from recovery/governance_parliament.py.

IMPORTANT — authority boundaries:
  1. This module SUPPLEMENTS ``backend.common.governance.enforce_fulfillment_policy()``
     and ``backend.common.governance.approval_decision()``. It does NOT replace them.
     The simple HIGH_RISK_TERMS gate in ``governance.py`` remains the first line of
     defence; the parliament is invoked *after* that gate flags a proposal as HIGH risk
     to provide an advisory multi-agent vote before escalating to an operator or Darwin.

  2. This module does NOT override Darwin. Darwin remains the canonical
     mutation-governance authority per Canon §8. The parliament is a Samus-internal
     advisory check for strategy-plane decisions only. When a vote fails the caller
     should escalate to Darwin via /gate rather than blocking the action unilaterally.

  3. "Advisory" means: a VETO verdict signals the caller to halt and escalate; an
     APPROVE verdict does not authorise actions that require Darwin sign-off under
     Canon §8 (mutation_apply, capability_register, governance_modify).

Canonical relationship:
  - Expands §8 Stage 5 multi-analyst review (threat_assessor / compliance_officer /
    operational_risk panel) generalised to N-agent parliament.
  - Pairs with three_plane_authority_model.md (Parliament lives in PLANE 1).
  - Pairs with autonomy_tier_model.md (LEVEL 5 requires parliament approval).
  - Gates Stage 7 apply when risk >= 6.0 per canonical §8 routing matrix.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Vote",
    "ParliamentAgent",
    "ParliamentVerdict",
    "GovernanceParliament",
]

_LOG = logging.getLogger("samus.common.governance_parliament")


class Vote(Enum):
    """Three-state vote outcome."""

    APPROVE = "approve"
    ABSTAIN = "abstain"
    VETO = "veto"


@dataclass
class ParliamentAgent:
    """An advisory voting agent within the governance parliament.

    Fields
    ------
    name
        Unique identifier for this agent within a parliament instance.
    authority_scope
        List of action labels this agent is eligible to vote on.
        Examples::

            ["mutation_apply", "code_modify"]        # code-mutation specialist
            ["governance_modify", "rule_amend"]      # policy specialist
            ["external_egress", "network_action"]    # security specialist
            ["resource_allocation", "compute_budget"]# economic specialist

    authority_weight
        Multiplier applied to this agent's vote weight (default 1.0).
        Useful for elevating senior agents without altering trust_score.
    confidence
        Per-agent confidence in its own assessments (0.0–1.0). Lower confidence
        agents have proportionally less weight in the tally.
    veto_power
        When True a single VETO vote from this agent overrides the majority and
        forces a VETO verdict regardless of other votes. Use only for safety-critical
        specialists such as a security agent guarding external egress.
    trust_score
        Reputation track-record (0.0–2.0; defaults 1.0). Adjusted post-vote by the
        parliament based on alignment with final outcomes. Agents that consistently
        align with results earn trust; those that deviate lose it.
    """

    name: str
    authority_scope: list[str]
    authority_weight: float = 1.0
    confidence: float = 1.0
    veto_power: bool = False
    trust_score: float = 1.0

    # Internal — not part of public API surface.
    _vote_history: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def eligible(self, action: str) -> bool:
        """Return True if this agent may vote on ``action``."""
        return action in self.authority_scope

    def effective_weight(self) -> float:
        """Effective vote weight: authority_weight × confidence × trust_score.

        Floor at 0.0 so trust_score of 0 produces exactly zero weight
        (effectively silences the agent without removing it from the registry).
        """
        return max(0.0, self.authority_weight * self.confidence * self.trust_score)

    def cast_vote(self, action: str, risk_score: float) -> Vote:
        """Default heuristic decision.

        Threshold = confidence × trust_score. If threshold > risk_score → APPROVE.
        If threshold < risk_score × 0.5 → VETO. Otherwise ABSTAIN.

        Override or monkey-patch in production to wire an LLM reasoning engine
        or a rule set per Canon §8 Stage 5.
        """
        if not self.eligible(action):
            return Vote.ABSTAIN
        threshold = self.confidence * self.trust_score
        if threshold > risk_score:
            return Vote.APPROVE
        if threshold < risk_score * 0.5:
            return Vote.VETO
        return Vote.ABSTAIN


@dataclass
class ParliamentVerdict:
    """Result of a parliament vote.

    ``outcome``         — "approve" | "abstain" | "veto"
    ``approve_weight``  — sum of weights on APPROVE side
    ``veto_weight``     — sum of weights on VETO side
    ``abstain_weight``  — sum of weights on ABSTAIN side
    ``total_weight``    — total eligible weight (0.0 if no eligible agents)
    ``approval_ratio``  — approve_weight / total_weight (0.0 if total == 0)
    ``threshold``       — required approval_ratio for this risk level
    ``eligible_count``  — number of agents eligible to vote
    ``voted_count``     — number of agents that cast non-ABSTAIN votes
    ``hard_veto``       — True if a veto_power agent forced VETO
    ``vote_detail``     — per-agent breakdown for audit trail
    ``reason``          — human-readable explanation of the verdict
    """

    outcome: str
    approve_weight: float
    veto_weight: float
    abstain_weight: float
    total_weight: float
    approval_ratio: float
    threshold: float
    eligible_count: int
    voted_count: int
    hard_veto: bool
    vote_detail: list[dict[str, Any]]
    reason: str


class GovernanceParliament:
    """Advisory multi-agent voting parliament.

    Advisory layer: binding governance authority remains with Darwin per Canon §8.

    Parameters
    ----------
    agents
        Initial agent roster. Agents can be added post-construction via
        ``add_agent()``.
    quorum
        Fraction of registered agents that must be eligible to vote before the
        vote proceeds. Default 0.5 (at least half of all agents must cover the
        action domain).
    base_approval_threshold
        Baseline weighted-approval ratio required for APPROVE. Default 0.67.
    risk_curve_cap
        Maximum additional threshold contributed by high risk_score.
        Threshold = min(base + risk_score × risk_slope, risk_curve_cap).
    risk_slope
        Multiplier applied to risk_score when raising the threshold. Default 0.25.
    reputation_gain
        Trust_score increment for agents that align with the final verdict.
    reputation_loss
        Trust_score decrement for agents that diverge from the final verdict.
    """

    def __init__(
        self,
        agents: list[ParliamentAgent] | None = None,
        *,
        quorum: float = 0.5,
        base_approval_threshold: float = 0.67,
        risk_curve_cap: float = 0.95,
        risk_slope: float = 0.25,
        reputation_gain: float = 0.02,
        reputation_loss: float = 0.05,
    ) -> None:
        self._agents: list[ParliamentAgent] = list(agents or [])
        self.quorum = quorum
        self.base_approval_threshold = base_approval_threshold
        self.risk_curve_cap = risk_curve_cap
        self.risk_slope = risk_slope
        self.reputation_gain = reputation_gain
        self.reputation_loss = reputation_loss
        self.vote_log: list[dict[str, Any]] = []

    def add_agent(self, agent: ParliamentAgent) -> None:
        """Register a new voting agent. Duplicate names are allowed (rare; avoid)."""
        self._agents.append(agent)
        _LOG.debug("governance_parliament: registered agent %s", agent.name)

    @property
    def agents(self) -> list[ParliamentAgent]:
        """Read-only view of registered agents."""
        return list(self._agents)

    def _risk_adjusted_threshold(self, risk_score: float) -> float:
        """Higher risk → higher required approval bar, capped at risk_curve_cap."""
        return min(
            self.risk_curve_cap,
            self.base_approval_threshold + risk_score * self.risk_slope,
        )

    def vote(self, proposal: dict[str, Any]) -> ParliamentVerdict:
        """Run a vote on ``proposal``.

        ``proposal`` must contain:
          ``"action"``     (str)   — action label matched against authority_scope.
          ``"risk_score"`` (float) — numeric risk level; higher = stricter threshold.

        Other keys are recorded in the audit log but not used in vote logic.

        Returns a :class:`ParliamentVerdict`. The caller is responsible for acting
        on the verdict — the parliament itself does not block execution.
        """
        action: str = proposal.get("action", "")
        risk_score: float = float(proposal.get("risk_score", 0.5))
        timestamp = time.time()

        eligible = [a for a in self._agents if a.eligible(action)]

        quorum_required = max(1, int(len(self._agents) * self.quorum)) if self._agents else 1

        # No eligible agents → advisory veto (cannot approve the unknown).
        if not eligible:
            verdict = self._build_verdict(
                outcome="veto",
                approve_w=0.0,
                veto_w=0.0,
                abstain_w=0.0,
                total_w=0.0,
                threshold=self._risk_adjusted_threshold(risk_score),
                eligible_count=0,
                voted_count=0,
                hard_veto=False,
                detail=[],
                reason="no_eligible_agents",
            )
            self._log(action, risk_score, verdict, timestamp, proposal)
            self._publish_verdict(action, risk_score, verdict)
            return verdict

        # Quorum check.
        if len(eligible) < quorum_required:
            verdict = self._build_verdict(
                outcome="veto",
                approve_w=0.0,
                veto_w=0.0,
                abstain_w=0.0,
                total_w=0.0,
                threshold=self._risk_adjusted_threshold(risk_score),
                eligible_count=len(eligible),
                voted_count=0,
                hard_veto=False,
                detail=[],
                reason=f"quorum_not_met: {len(eligible)}/{quorum_required}",
            )
            self._log(action, risk_score, verdict, timestamp, proposal)
            self._publish_verdict(action, risk_score, verdict)
            return verdict

        # Collect votes.
        threshold = self._risk_adjusted_threshold(risk_score)
        approve_w = 0.0
        veto_w = 0.0
        abstain_w = 0.0
        total_w = 0.0
        voted_count = 0
        hard_veto = False
        detail: list[dict[str, Any]] = []

        for agent in eligible:
            agent_vote = agent.cast_vote(action, risk_score)
            weight = agent.effective_weight()
            total_w += weight

            if agent_vote == Vote.APPROVE:
                approve_w += weight
                voted_count += 1
            elif agent_vote == Vote.VETO:
                veto_w += weight
                voted_count += 1
                if agent.veto_power:
                    hard_veto = True
                    _LOG.info(
                        "governance_parliament: hard veto by %s on action=%s",
                        agent.name,
                        action,
                    )
            else:  # ABSTAIN
                abstain_w += weight

            detail.append({
                "agent": agent.name,
                "vote": agent_vote.value,
                "weight": weight,
                "veto_power": agent.veto_power,
            })

        # Determine outcome.
        if hard_veto:
            outcome = "veto"
            reason = "hard_veto_by_power_agent"
        elif total_w == 0.0:
            outcome = "abstain"
            reason = "zero_effective_weight"
        else:
            approval_ratio = approve_w / total_w
            if approval_ratio >= threshold:
                outcome = "approve"
                reason = f"approval_ratio={approval_ratio:.3f} >= threshold={threshold:.3f}"
            else:
                net = approve_w - veto_w
                if net <= 0:
                    outcome = "veto"
                    reason = f"approval_ratio={approval_ratio:.3f} < threshold={threshold:.3f}"
                else:
                    outcome = "abstain"
                    reason = f"approval_ratio={approval_ratio:.3f} < threshold={threshold:.3f}; net positive but insufficient"

        verdict = self._build_verdict(
            outcome=outcome,
            approve_w=approve_w,
            veto_w=veto_w,
            abstain_w=abstain_w,
            total_w=total_w,
            threshold=threshold,
            eligible_count=len(eligible),
            voted_count=voted_count,
            hard_veto=hard_veto,
            detail=detail,
            reason=reason,
        )

        self._log(action, risk_score, verdict, timestamp, proposal)
        self._update_reputation(detail, outcome)
        self._publish_verdict(action, risk_score, verdict)

        _LOG.info(
            "governance_parliament: action=%s risk=%.2f outcome=%s agents=%d/%d",
            action,
            risk_score,
            outcome,
            voted_count,
            len(eligible),
        )
        return verdict

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _publish_verdict(
        action: str, risk_score: float, verdict: "ParliamentVerdict"
    ) -> None:
        """Fan a verdict out to the cross-agent Quorum Hub. Best-effort.

        Only VETO / ABSTAIN outcomes actually fan out (the publisher filters);
        clean APPROVE verdicts stay local. The whole call is gated on
        SAMUS_QUORUM_PUBLISH_ENABLED inside the hub client and is fully
        fail-open — a hub outage, a disabled flag, or any error here MUST NOT
        affect the vote result. Import is lazy so the parliament has no hard
        dependency on the publisher at module-load time.
        """
        try:
            from backend.common.quorum_publisher import publish_parliament_verdict

            publish_parliament_verdict(
                verdict, action=action, risk_score=risk_score, caller="samus"
            )
        except Exception:  # noqa: BLE001 — publishing must never break the vote path
            _LOG.debug(
                "governance_parliament: hub publish skipped/failed for action=%s",
                action,
            )

    @staticmethod
    def _build_verdict(
        *,
        outcome: str,
        approve_w: float,
        veto_w: float,
        abstain_w: float,
        total_w: float,
        threshold: float,
        eligible_count: int,
        voted_count: int,
        hard_veto: bool,
        detail: list[dict[str, Any]],
        reason: str,
    ) -> ParliamentVerdict:
        approval_ratio = (approve_w / total_w) if total_w > 0 else 0.0
        return ParliamentVerdict(
            outcome=outcome,
            approve_weight=approve_w,
            veto_weight=veto_w,
            abstain_weight=abstain_w,
            total_weight=total_w,
            approval_ratio=approval_ratio,
            threshold=threshold,
            eligible_count=eligible_count,
            voted_count=voted_count,
            hard_veto=hard_veto,
            vote_detail=detail,
            reason=reason,
        )

    def _log(
        self,
        action: str,
        risk_score: float,
        verdict: ParliamentVerdict,
        timestamp: float,
        proposal: dict[str, Any],
    ) -> None:
        self.vote_log.append({
            "action": action,
            "risk_score": risk_score,
            "outcome": verdict.outcome,
            "approval_ratio": verdict.approval_ratio,
            "threshold": verdict.threshold,
            "hard_veto": verdict.hard_veto,
            "eligible_count": verdict.eligible_count,
            "voted_count": verdict.voted_count,
            "vote_detail": verdict.vote_detail,
            "reason": verdict.reason,
            "proposal_meta": {k: v for k, v in proposal.items()
                              if k not in ("action", "risk_score")},
            "timestamp": timestamp,
        })

    def _update_reputation(
        self, detail: list[dict[str, Any]], final_outcome: str
    ) -> None:
        """Adjust trust scores post-vote based on alignment with the final verdict."""
        for entry in detail:
            if entry["vote"] == Vote.ABSTAIN.value:
                continue
            agent_obj = next(
                (a for a in self._agents if a.name == entry["agent"]), None
            )
            if agent_obj is None:
                continue
            aligned = (
                (entry["vote"] == Vote.APPROVE.value and final_outcome == "approve")
                or (entry["vote"] == Vote.VETO.value and final_outcome == "veto")
            )
            if aligned:
                agent_obj.trust_score = min(2.0, agent_obj.trust_score + self.reputation_gain)
            else:
                agent_obj.trust_score = max(0.0, agent_obj.trust_score - self.reputation_loss)
