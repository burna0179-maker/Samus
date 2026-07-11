"""Canonical call-outcome taxonomy — backend.crm.call_outcomes.

The single source of truth shared by the operator hand-call path
(``log_call``) and the automated Vapi agent path (``voice.service``).
"""
from __future__ import annotations

from backend.crm.call_outcomes import (
    OUTCOME_TO_STATE,
    VALID_OUTCOMES,
    is_valid_outcome,
    state_for_outcome,
)


def test_taxonomy_carries_the_canonical_outcomes():
    # 9 call outcomes + "noted" (operator-engaged-but-no-classification,
    # added 2026-06-22 for the forge-ui cc-notes surface) = 10 total.
    assert set(OUTCOME_TO_STATE) == {
        "booked", "follow_up", "disqualified", "gatekeeper", "not_interested",
        "hung_up", "no_answer", "voicemail", "do_not_call", "noted",
    }
    assert VALID_OUTCOMES == tuple(OUTCOME_TO_STATE)


def test_connected_outcomes_conclude_the_call():
    """Connected-call outcomes map to 'completed' — the nuance rides in
    last_outcome (the booked / follow_up / disqualified precedent)."""
    for outcome in ("booked", "follow_up", "disqualified",
                    "not_interested", "hung_up"):
        assert state_for_outcome(outcome) == "completed", outcome


def test_gatekeeper_is_the_one_non_terminal_state():
    """gatekeeper did NOT conclude — its own state keeps the prospect callable."""
    assert state_for_outcome("gatekeeper") == "gatekeeper"


def test_mechanical_outcomes_keep_their_own_state():
    assert state_for_outcome("no_answer") == "no_answer"
    assert state_for_outcome("voicemail") == "voicemail"
    assert state_for_outcome("do_not_call") == "do_not_call"


def test_unknown_or_empty_outcome_defaults_to_completed():
    """A call happened with no clearer signal — it still concluded."""
    assert state_for_outcome("") == "completed"
    assert state_for_outcome("not_a_real_outcome") == "completed"


def test_is_valid_outcome():
    assert is_valid_outcome("gatekeeper") is True
    assert is_valid_outcome("booked") is True
    assert is_valid_outcome("") is False
    assert is_valid_outcome("book_call") is False   # Vapi verb, not canonical
