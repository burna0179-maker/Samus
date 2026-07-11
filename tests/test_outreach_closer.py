"""Tests for the outreach closer FSM — backend/outreach/closer.py.

16 tests covering:
  - module-level constants
  - next_state() pure transition function (all branches)
  - run_closer_step() per-turn decision function (action selection + return shape)
  - app endpoint integration for advance_call_state action
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_initial_state_constants_are_correct():
    from backend.outreach.closer import INITIAL_STATE, TERMINAL_STATE, STATES

    assert INITIAL_STATE == "open"
    assert TERMINAL_STATE == "exit"
    assert STATES[0] == "open"
    assert STATES[-1] == "exit"
    assert len(STATES) == 7


# ---------------------------------------------------------------------------
# next_state — pure transition function
# ---------------------------------------------------------------------------


def test_next_state_open_to_pitch():
    from backend.outreach.closer import next_state

    assert next_state("open", {}) == "pitch"


def test_next_state_pitch_to_engage():
    from backend.outreach.closer import next_state

    assert next_state("pitch", {}) == "engage"


def test_next_state_engage_with_objection_routes_to_handle():
    from backend.outreach.closer import next_state

    assert next_state("engage", {"objection": True}) == "handle_objection"


def test_next_state_engage_without_objection_routes_to_close_attempt():
    from backend.outreach.closer import next_state

    assert next_state("engage", {"objection": None}) == "close_attempt"
    assert next_state("engage", {}) == "close_attempt"


def test_next_state_handle_objection_to_close_attempt():
    from backend.outreach.closer import next_state

    assert next_state("handle_objection", {}) == "close_attempt"
    assert next_state("handle_objection", {"objection": True}) == "close_attempt"


def test_next_state_close_attempt_with_resistance_routes_to_fallback():
    from backend.outreach.closer import next_state

    assert next_state("close_attempt", {"resistance": True}) == "fallback"


def test_next_state_close_attempt_without_resistance_routes_to_exit():
    from backend.outreach.closer import next_state

    assert next_state("close_attempt", {"resistance": False}) == "exit"
    assert next_state("close_attempt", {}) == "exit"


def test_next_state_fallback_to_exit():
    from backend.outreach.closer import next_state

    assert next_state("fallback", {}) == "exit"


def test_next_state_unknown_state_returns_exit():
    from backend.outreach.closer import next_state

    assert next_state("bogus_state", {}) == "exit"
    assert next_state("", {}) == "exit"
    assert next_state("exit", {}) == "exit"


# ---------------------------------------------------------------------------
# run_closer_step — per-turn decision function
# ---------------------------------------------------------------------------

_INTEL = {"products": {"primary": "seo", "secondary": "ads"}}
_INTEL_NO_SECONDARY = {"products": {"primary": "seo"}}


def test_run_closer_step_open_emits_deliver_opener():
    from backend.outreach.closer import run_closer_step

    result = run_closer_step("open", "", _INTEL)
    assert result["action"] == "deliver_opener"
    assert result["current_state"] == "open"
    assert result["next_state"] == "pitch"
    assert result["primary_product"] == "seo"
    assert result["secondary_product"] == "ads"


def test_run_closer_step_close_attempt_action_includes_primary_product():
    from backend.outreach.closer import run_closer_step

    result = run_closer_step("close_attempt", "", _INTEL)
    assert result["action"] == "attempt_close_on_seo"
    assert result["next_state"] == "exit"


def test_run_closer_step_fallback_uses_secondary_when_present():
    from backend.outreach.closer import run_closer_step

    result = run_closer_step("fallback", "", _INTEL)
    assert result["action"] == "pivot_to_ads"
    assert result["next_state"] == "exit"


def test_run_closer_step_fallback_falls_back_to_audit_when_no_secondary():
    from backend.outreach.closer import run_closer_step

    result = run_closer_step("fallback", "", _INTEL_NO_SECONDARY)
    assert result["action"] == "offer_free_audit"
    assert result["secondary_product"] is None


def test_run_closer_step_handle_objection_uses_response_from_objection_result():
    from backend.outreach.closer import run_closer_step

    obj = {"detected": True, "response": "empathic_reframe"}
    result = run_closer_step("handle_objection", "", _INTEL, objection_result=obj)
    assert result["action"] == "empathic_reframe"
    assert result["next_state"] == "close_attempt"


def test_run_closer_step_handle_objection_falls_back_to_acknowledge_when_no_result():
    from backend.outreach.closer import run_closer_step

    result = run_closer_step("handle_objection", "", _INTEL, objection_result=None)
    assert result["action"] == "acknowledge"


def test_run_closer_step_engage_detects_resistance_from_user_input():
    """Resistance phrase in user_input propagates through next_state into the result."""
    from backend.outreach.closer import run_closer_step

    # Resistance during close_attempt triggers fallback via next_state
    result = run_closer_step("close_attempt", "not interested at all", _INTEL)
    assert result["next_state"] == "fallback"
    # No resistance — should go to exit
    result_no_resist = run_closer_step("close_attempt", "tell me more", _INTEL)
    assert result_no_resist["next_state"] == "exit"


# ---------------------------------------------------------------------------
# app endpoint integration — advance_call_state via /work dispatcher
# ---------------------------------------------------------------------------


def test_app_work_advance_call_state_returns_closer_step(monkeypatch):
    """POST /work with action=advance_call_state routes through closer.run_closer_step."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", "/tmp/test_closer_audit.jsonl")
    from fastapi.testclient import TestClient
    from backend.outreach.app import app

    client = TestClient(app)
    envelope = {
        "task_id": "test-closer-001",
        "payload": {
            "state": "open",
            "user_input": "",
            "intel": {"products": {"primary": "seo", "secondary": "ads"}},
            "objection_result": None,
        },
        "metadata": {"action": "advance_call_state"},
    }
    r = client.post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_state"] == "open"
    assert body["next_state"] == "pitch"
    assert body["action"] == "deliver_opener"
    assert body["primary_product"] == "seo"
    assert body["secondary_product"] == "ads"


def test_app_work_advance_call_state_missing_state_field_returns_422(monkeypatch):
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", "/tmp/test_closer_audit.jsonl")
    from fastapi.testclient import TestClient
    from backend.outreach.app import app

    client = TestClient(app)
    envelope = {
        "task_id": "test-closer-002",
        "payload": {
            "user_input": "",
            "intel": {"products": {"primary": "seo"}},
        },
        "metadata": {"action": "advance_call_state"},
    }
    r = client.post("/work", json=envelope)
    assert r.status_code == 422


def test_app_work_advance_call_state_missing_products_key_returns_422(monkeypatch):
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", "/tmp/test_closer_audit.jsonl")
    from fastapi.testclient import TestClient
    from backend.outreach.app import app

    client = TestClient(app)
    envelope = {
        "task_id": "test-closer-003",
        "payload": {
            "state": "open",
            "user_input": "",
            "intel": {},  # missing "products" key
        },
        "metadata": {"action": "advance_call_state"},
    }
    r = client.post("/work", json=envelope)
    assert r.status_code == 422
