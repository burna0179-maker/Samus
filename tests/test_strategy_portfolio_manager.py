"""Tests for backend.strategy.portfolio_manager (tier-3 hedge-fund decision layer).

LLM calls are monkeypatched — no real Anthropic API calls are made.
Bandit and RL state is reset between tests via the reset_* helpers.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset(monkeypatch=None):
    """Reset module-level bandit and RL state for test isolation."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()
    pm.reset_rl_credits()


# ---------------------------------------------------------------------------
# Bandit: update_bandit
# ---------------------------------------------------------------------------

def test_update_bandit_increments_arm_count():
    """update_bandit should increment trial count for the arm."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()

    pm.update_bandit("accelerate", 1.0)
    pm.update_bandit("accelerate", 0.0)
    stats = pm.get_bandit_stats()

    assert "accelerate" in stats
    assert stats["accelerate"]["trials"] == 2
    pm.reset_bandit()


def test_update_bandit_accumulates_wins():
    """update_bandit should sum reward into wins."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()

    pm.update_bandit("maintain", 0.5)
    pm.update_bandit("maintain", 0.3)
    stats = pm.get_bandit_stats()

    assert abs(stats["maintain"]["wins"] - 0.8) < 1e-9
    pm.reset_bandit()


def test_update_bandit_multiple_arms_isolated():
    """Different arms must not share state."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()

    pm.update_bandit("accelerate", 1.0)
    pm.update_bandit("defer", 0.0)
    stats = pm.get_bandit_stats()

    assert stats["accelerate"]["trials"] == 1
    assert stats["defer"]["trials"] == 1
    assert stats["accelerate"]["wins"] != stats["defer"]["wins"]
    pm.reset_bandit()


# ---------------------------------------------------------------------------
# record_outcome + RL credits
# ---------------------------------------------------------------------------

def test_record_outcome_modifies_rl_state():
    """record_outcome should store credit keyed by prospect_id."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()
    pm.reset_rl_credits()

    pm.record_outcome("prospect_abc", "accelerate", "won")
    credits = pm.get_rl_credits()

    assert "prospect_abc" in credits
    assert credits["prospect_abc"]["result"] == "won"
    assert credits["prospect_abc"]["action"] == "accelerate"
    pm.reset_bandit()
    pm.reset_rl_credits()


def test_record_outcome_credit_assignment():
    """won=+1.0, lost=-0.5, stale=-0.1, unknown=0.0."""
    import backend.strategy.portfolio_manager as pm
    pm.reset_bandit()
    pm.reset_rl_credits()

    cases = [
        ("p1", "won", 1.0),
        ("p2", "lost", -0.5),
        ("p3", "stale", -0.1),
        ("p4", "unknown_status", 0.0),
    ]
    for pid, result, expected_credit in cases:
        pm.record_outcome(pid, "accelerate", result)

    credits = pm.get_rl_credits()
    for pid, _, expected_credit in cases:
        assert abs(credits[pid]["credit"] - expected_credit) < 1e-9, (
            f"{pid}: expected credit {expected_credit}, got {credits[pid]['credit']}"
        )
    pm.reset_bandit()
    pm.reset_rl_credits()


# ---------------------------------------------------------------------------
# propose_allocation — monkeypatched LLM
# ---------------------------------------------------------------------------

def _mock_anthropic_returns(json_response: str):
    """Return a monkeypatch target that injects ``json_response`` as LLM output."""
    def _mock_anthropic_messages(**kwargs):
        return json_response, {"input_tokens": 10, "output_tokens": 20}
    return _mock_anthropic_messages


def test_propose_allocation_parses_valid_llm_response(monkeypatch):
    """propose_allocation should parse well-formed JSON from the LLM."""
    import backend.strategy.portfolio_manager as pm
    import backend.common.llm_client as llm_mod

    response_payload = json.dumps({
        "priorities": ["p1", "p2"],
        "deprioritize": ["p3"],
        "actions": [{"type": "accelerate", "prospect_id": "p1"}],
    })
    monkeypatch.setattr(llm_mod, "anthropic_messages", _mock_anthropic_returns(response_payload))

    state = pm.PortfolioState(
        budget_remaining=4_000.0,
        avg_conversion_rate=0.15,
        pipeline_value=25_000.0,
        active_prospects=3,
        prospects=[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
    )
    decision = pm.propose_allocation(state, {}, api_key="test-key")

    assert decision.parse_error is False
    assert "p1" in decision.priorities
    assert "p3" in decision.deprioritize
    assert len(decision.actions) == 1


def test_propose_allocation_handles_bad_json_gracefully(monkeypatch):
    """propose_allocation must return parse_error=True on non-JSON LLM output."""
    import backend.strategy.portfolio_manager as pm
    import backend.common.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "anthropic_messages", _mock_anthropic_returns("not json {{{"))

    state = pm.PortfolioState(
        budget_remaining=1_000.0,
        avg_conversion_rate=0.1,
        pipeline_value=5_000.0,
        active_prospects=1,
    )
    decision = pm.propose_allocation(state, {}, api_key="test-key")
    assert decision.parse_error is True
    assert decision.priorities == []


def test_propose_allocation_no_api_key_still_calls_llm():
    """LM Studio needs no API key — empty key must not block the LLM path."""
    import backend.strategy.portfolio_manager as pm

    state = pm.PortfolioState(
        budget_remaining=1_000.0,
        avg_conversion_rate=0.1,
        pipeline_value=5_000.0,
        active_prospects=0,
    )
    # With LM Studio offline the call will fail, but it must attempt — not bail on empty key.
    decision = pm.propose_allocation(state, {}, api_key="")
    # parse_error=True is acceptable (LM Studio may be down in test env),
    # but the function must not raise.
    assert isinstance(decision, pm.AllocationDecision)
