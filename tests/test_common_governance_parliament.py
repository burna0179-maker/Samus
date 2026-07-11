"""Tests for backend.common.governance_parliament.

Validates advisory voting behaviour. All tests confirm that the parliament
is an advisory layer only — it does not claim mutation-governance authority
(that belongs to Darwin per Canon §8).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(name, scope, *, confidence=1.0, trust_score=1.0, veto_power=False, weight=1.0):
    from backend.common.governance_parliament import ParliamentAgent
    return ParliamentAgent(
        name=name,
        authority_scope=scope,
        authority_weight=weight,
        confidence=confidence,
        veto_power=veto_power,
        trust_score=trust_score,
    )


def _parliament(*agents, quorum=0.0):
    """Build a GovernanceParliament with optional quorum override."""
    from backend.common.governance_parliament import GovernanceParliament
    return GovernanceParliament(list(agents), quorum=quorum)


# ---------------------------------------------------------------------------
# add_agent
# ---------------------------------------------------------------------------

def test_add_agent_registers_correctly():
    """add_agent should make the agent available in parliament.agents."""
    from backend.common.governance_parliament import GovernanceParliament, ParliamentAgent

    p = GovernanceParliament()
    agent = ParliamentAgent(name="sec", authority_scope=["external_egress"])
    p.add_agent(agent)

    assert any(a.name == "sec" for a in p.agents)


def test_add_agent_does_not_affect_existing_agents():
    """Adding a new agent must not mutate pre-existing agent list."""
    a1 = _make_agent("a1", ["code_modify"])
    p = _parliament(a1, quorum=0.0)

    a2 = _make_agent("a2", ["network_action"])
    p.add_agent(a2)

    names = [a.name for a in p.agents]
    assert "a1" in names
    assert "a2" in names
    assert len(names) == 2


# ---------------------------------------------------------------------------
# vote — three-agent tally
# ---------------------------------------------------------------------------

def test_vote_three_agents_majority_approve():
    """Three eligible agents: two APPROVE → verdict should be 'approve'."""
    # confidence > risk_score → APPROVE; all three have confidence=1.0, risk=0.3
    agents = [
        _make_agent(f"agent{i}", ["external_egress"], confidence=1.0, trust_score=1.0)
        for i in range(3)
    ]
    p = _parliament(*agents, quorum=0.0)
    verdict = p.vote({"action": "external_egress", "risk_score": 0.3})

    assert verdict.outcome == "approve"
    assert verdict.eligible_count == 3
    assert verdict.voted_count == 3


def test_vote_tally_captures_all_votes():
    """vote_detail must have one entry per eligible agent."""
    agents = [
        _make_agent(f"a{i}", ["compute_budget"]) for i in range(4)
    ]
    p = _parliament(*agents, quorum=0.0)
    verdict = p.vote({"action": "compute_budget", "risk_score": 0.2})

    assert len(verdict.vote_detail) == 4
    agent_names = {d["agent"] for d in verdict.vote_detail}
    assert agent_names == {"a0", "a1", "a2", "a3"}


def test_vote_with_no_eligible_agents_returns_veto():
    """When no agent covers the action the verdict must be 'veto'."""
    agents = [_make_agent("a1", ["code_modify"])]
    p = _parliament(*agents, quorum=0.0)
    verdict = p.vote({"action": "network_action", "risk_score": 0.5})

    assert verdict.outcome == "veto"
    assert verdict.eligible_count == 0
    assert "no_eligible_agents" in verdict.reason


# ---------------------------------------------------------------------------
# veto_power hard veto
# ---------------------------------------------------------------------------

def test_veto_power_agent_blocks_majority():
    """A veto_power=True agent casting VETO must force 'veto' outcome even if others APPROVE."""
    # Two approvers + one veto_power agent with low confidence (will VETO).
    approver1 = _make_agent("approver1", ["mutation_apply"], confidence=1.0)
    approver2 = _make_agent("approver2", ["mutation_apply"], confidence=1.0)
    # confidence=0.1 < risk_score=0.8 * 0.5=0.4 → VETO branch
    blocker = _make_agent("blocker", ["mutation_apply"], confidence=0.1,
                          veto_power=True, trust_score=1.0)

    p = _parliament(approver1, approver2, blocker, quorum=0.0)
    verdict = p.vote({"action": "mutation_apply", "risk_score": 0.8})

    assert verdict.outcome == "veto"
    assert verdict.hard_veto is True


# ---------------------------------------------------------------------------
# trust_score=0 agent is silenced
# ---------------------------------------------------------------------------

def test_zero_trust_agent_has_no_weight():
    """An agent with trust_score=0 should contribute zero effective weight."""
    from backend.common.governance_parliament import ParliamentAgent

    agent = ParliamentAgent(
        name="untrusted",
        authority_scope=["code_modify"],
        authority_weight=1.0,
        confidence=1.0,
        trust_score=0.0,
    )
    assert agent.effective_weight() == 0.0


def test_vote_ignores_zero_trust_agent():
    """A zero-trust agent's APPROVE vote should not shift the verdict."""
    # One real agent (will VETO at risk=0.9), one zero-trust agent (would APPROVE)
    veto_agent = _make_agent("veto_a", ["code_modify"], confidence=0.1, trust_score=1.0)
    zero_agent = _make_agent("zero_a", ["code_modify"], confidence=1.0, trust_score=0.0)

    p = _parliament(veto_agent, zero_agent, quorum=0.0)
    verdict = p.vote({"action": "code_modify", "risk_score": 0.9})

    # veto_agent: confidence(0.1) < risk(0.9)*0.5=0.45 → VETO; weight contributes
    # zero_agent: weight=0 → no contribution
    # Net approve_weight=0, veto_weight from veto_agent → expect veto
    assert verdict.outcome == "veto"


# ---------------------------------------------------------------------------
# Advisory framing — parliament does not claim Darwin's authority
# ---------------------------------------------------------------------------

def test_parliament_does_not_override_darwin_scope():
    """The parliament module must export no symbol suggesting mutation authority."""
    import backend.common.governance_parliament as gp_mod

    # These are the only public names the module should export.
    public = set(gp_mod.__all__)
    mutation_authority_names = {
        "apply_mutation",
        "approve_capability_register",
        "enforce_governance",
        "grant_mutation_authority",
    }
    # None of the mutation-authority names should appear in __all__.
    overlap = public & mutation_authority_names
    assert not overlap, (
        f"governance_parliament must not claim mutation authority; found: {overlap}"
    )


def test_parliament_verdict_does_not_bypass_darwin(monkeypatch):
    """An 'approve' verdict must not contain any darwin-bypass flag."""
    agent = _make_agent("a1", ["resource_allocation"], confidence=1.0)
    p = _parliament(agent, quorum=0.0)
    verdict = p.vote({"action": "resource_allocation", "risk_score": 0.2})

    # Verdict should approve (confidence > risk) but have no darwin-bypass attribute.
    assert verdict.outcome == "approve"
    assert not hasattr(verdict, "darwin_approved")
    assert not hasattr(verdict, "mutation_granted")
    assert not hasattr(verdict, "bypass_darwin")
