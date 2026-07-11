"""Unit tests for the CRM opportunity pipeline FSM (pure functions)."""
from __future__ import annotations

import pytest

from backend.crm.pipeline import (
    STAGE_PROBABILITIES,
    TERMINAL_STAGES,
    WON_TERMINAL_STAGES,
    next_allowed_stages,
    validate_transition,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_terminal_stages_are_the_three_closed_stages():
    assert TERMINAL_STAGES == {
        "closed_won", "closed_won_retainer", "closed_lost",
    }


def test_won_terminal_stages():
    assert WON_TERMINAL_STAGES == {"closed_won", "closed_won_retainer"}
    # closed_lost is terminal but NOT a win
    assert "closed_lost" not in WON_TERMINAL_STAGES


def test_stage_probabilities_cover_all_seven_stages():
    assert set(STAGE_PROBABILITIES) == {
        "new", "qualified", "proposal", "negotiation",
        "closed_won", "closed_won_retainer", "closed_lost",
    }


def test_stage_probabilities_are_monotonic():
    assert STAGE_PROBABILITIES["new"] < STAGE_PROBABILITIES["qualified"]
    assert STAGE_PROBABILITIES["qualified"] < STAGE_PROBABILITIES["proposal"]
    assert STAGE_PROBABILITIES["proposal"] < STAGE_PROBABILITIES["negotiation"]
    assert STAGE_PROBABILITIES["negotiation"] < STAGE_PROBABILITIES["closed_won"]
    assert STAGE_PROBABILITIES["closed_won"] == 1.0
    assert STAGE_PROBABILITIES["closed_won_retainer"] == 1.0
    assert STAGE_PROBABILITIES["closed_lost"] == 0.0


# ---------------------------------------------------------------------------
# next_allowed_stages
# ---------------------------------------------------------------------------

def test_next_allowed_from_new_includes_forward_and_lost():
    allowed = next_allowed_stages("new")
    assert "qualified" in allowed
    assert "proposal" in allowed
    assert "negotiation" in allowed
    assert "closed_lost" in allowed
    # Can't directly jump from new to closed_won
    assert "closed_won" not in allowed


def test_next_allowed_from_qualified():
    allowed = next_allowed_stages("qualified")
    assert allowed == {
        "proposal", "negotiation",
        "closed_won", "closed_won_retainer", "closed_lost",
    }


def test_next_allowed_from_proposal():
    allowed = next_allowed_stages("proposal")
    assert allowed == {
        "negotiation", "closed_won", "closed_won_retainer", "closed_lost",
    }


def test_next_allowed_from_negotiation():
    allowed = next_allowed_stages("negotiation")
    assert allowed == {"closed_won", "closed_won_retainer", "closed_lost"}


def test_next_allowed_from_closed_won_is_empty():
    assert next_allowed_stages("closed_won") == set()


def test_next_allowed_from_closed_won_retainer_is_empty():
    assert next_allowed_stages("closed_won_retainer") == set()


def test_next_allowed_from_closed_lost_is_empty():
    assert next_allowed_stages("closed_lost") == set()


# ---------------------------------------------------------------------------
# validate_transition — legal cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current,target", [
    ("new", "qualified"),
    ("new", "proposal"),
    ("new", "negotiation"),
    ("new", "closed_lost"),
    ("qualified", "proposal"),
    ("qualified", "negotiation"),
    ("qualified", "closed_won"),
    ("qualified", "closed_won_retainer"),
    ("qualified", "closed_lost"),
    ("proposal", "negotiation"),
    ("proposal", "closed_won"),
    ("proposal", "closed_won_retainer"),
    ("proposal", "closed_lost"),
    ("negotiation", "closed_won"),
    ("negotiation", "closed_won_retainer"),
    ("negotiation", "closed_lost"),
])
def test_validate_transition_legal(current, target):
    ok, reason = validate_transition(current, target)
    assert ok is True, f"expected {current}->{target} to be legal, got {reason}"
    assert reason is None


# ---------------------------------------------------------------------------
# validate_transition — illegal cases
# ---------------------------------------------------------------------------

def test_validate_transition_same_stage_is_noop():
    ok, reason = validate_transition("new", "new")
    assert ok is False
    assert reason == "noop_same_stage"


def test_validate_transition_new_to_closed_won_is_illegal():
    ok, reason = validate_transition("new", "closed_won")
    assert ok is False
    assert reason and "illegal_transition" in reason


def test_validate_transition_new_to_closed_won_retainer_is_illegal():
    # Same geometry as closed_won — can't skip straight from `new`.
    ok, reason = validate_transition("new", "closed_won_retainer")
    assert ok is False
    assert reason and "illegal_transition" in reason


def test_validate_transition_from_closed_won_retainer_rejected():
    ok, reason = validate_transition("closed_won_retainer", "qualified")
    assert ok is False
    assert reason and "terminal_stage" in reason


def test_validate_transition_proposal_to_qualified_is_backwards():
    ok, reason = validate_transition("proposal", "qualified")
    assert ok is False
    assert reason and "illegal_transition" in reason


def test_validate_transition_from_closed_won_rejected():
    ok, reason = validate_transition("closed_won", "qualified")
    assert ok is False
    assert reason and "terminal_stage" in reason


def test_validate_transition_from_closed_lost_rejected():
    ok, reason = validate_transition("closed_lost", "new")
    assert ok is False
    assert reason and "terminal_stage" in reason


def test_validate_transition_unknown_target_rejected():
    ok, reason = validate_transition("new", "bogus")  # type: ignore[arg-type]
    assert ok is False
    assert reason and "unknown_target" in reason


def test_validate_transition_negotiation_to_proposal_backwards():
    ok, reason = validate_transition("negotiation", "proposal")
    assert ok is False
    assert reason and "illegal_transition" in reason
