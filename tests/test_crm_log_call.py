"""Operator call-logging — backend.crm.log_call."""

from __future__ import annotations

import json


def _patch_crm(monkeypatch, *, conv_ok=True, state_ok=True, prior=None, opp_status="created"):
    """Stub the CRM service layer so tests never touch DynamoDB."""
    from backend.crm import service as crm_service
    from backend.crm.models import CreateOpportunityResult

    captured: dict = {}

    def _upsert_conversation(conv):
        captured["conversation"] = conv
        return conv_ok

    def _upsert_call_state(state):
        captured["call_state"] = state
        return state_ok

    def _create_opportunity(req):
        captured["opportunity_request"] = req
        return CreateOpportunityResult(
            status=opp_status,
            opportunity_id="opty_test1" if opp_status == "created" else "",
            ts="2026-05-20T00:00:00Z",
            error=None if opp_status == "created" else "ddb_put_failed",
        )

    monkeypatch.setattr(crm_service, "upsert_conversation", _upsert_conversation)
    monkeypatch.setattr(crm_service, "upsert_call_state", _upsert_call_state)
    monkeypatch.setattr(crm_service, "get_call_state", lambda pid: prior)
    monkeypatch.setattr(crm_service, "create_opportunity", _create_opportunity)
    return captured


def test_log_call_writes_conversation_and_call_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    captured = _patch_crm(monkeypatch)
    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_test1",
        company="Acme HVAC",
        outcome="booked",
        notes="owner booked an audit Thu 2pm",
        phone="(555) 1",
    )
    assert result["ok"] is True
    assert result["conversation_persisted"] is True
    assert result["call_state_persisted"] is True

    conv = captured["conversation"]
    assert conv.prospect_id == "pr_test1"
    assert conv.channel == "call"
    assert conv.outcome == "booked"
    assert conv.source == "operator"
    assert conv.transcript == "owner booked an audit Thu 2pm"

    state = captured["call_state"]
    assert state.prospect_id == "pr_test1"
    assert state.state == "completed"  # booked -> completed
    assert state.last_outcome == "booked"
    assert state.attempt_count == 1  # no prior state

    # booked -> a tracked Opportunity is opened
    assert result["opportunity_id"] == "opty_test1"
    opp_req = captured["opportunity_request"]
    assert opp_req.prospect_id == "pr_test1"
    assert opp_req.intent_score == 85


def test_log_call_increments_attempt_count(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.crm.models import CallState

    prior = CallState(prospect_id="pr_x", attempt_count=2)
    captured = _patch_crm(monkeypatch, prior=prior)
    from backend.crm.log_call import log_call

    result = log_call(prospect_id="pr_x", outcome="no_answer", notes="")
    assert result["ok"] is True
    assert captured["call_state"].attempt_count == 3
    assert captured["call_state"].state == "no_answer"  # no_answer -> no_answer
    assert result["opportunity_id"] == ""  # not booked -> no opportunity
    assert "opportunity_request" not in captured


# --- outcome granularity added 2026-05-21 (gatekeeper / not_interested /
#     hung_up) — see _OUTCOME_TO_STATE in log_call.py ------------------------


def test_log_call_gatekeeper_is_non_terminal_state(tmp_path, monkeypatch):
    """gatekeeper is the one new outcome that keeps the prospect callable."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    captured = _patch_crm(monkeypatch)
    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_gk", outcome="gatekeeper", notes="gatekeeper; DM is Heidi Murray"
    )
    assert result["ok"] is True
    # Its own non-terminal state — NOT "completed" — so the retry pool keeps it.
    assert captured["call_state"].state == "gatekeeper"
    assert captured["call_state"].last_outcome == "gatekeeper"
    assert result["opportunity_id"] == ""  # not booked -> no opportunity


def test_log_call_not_interested_maps_to_completed(tmp_path, monkeypatch):
    """not_interested is a concluded connected call — completed, soft no."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    captured = _patch_crm(monkeypatch)
    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_ni", outcome="not_interested", notes="owner declined, not now"
    )
    assert result["ok"] is True
    assert captured["call_state"].state == "completed"
    assert captured["call_state"].last_outcome == "not_interested"
    assert result["opportunity_id"] == ""


def test_log_call_hung_up_is_completed_not_no_answer(tmp_path, monkeypatch):
    """Regression: an answered-then-hung-up call must NOT be a no_answer.

    no_answer drives ring-out/redial retry logic; a pickup-then-rejection is a
    contact, so hung_up maps to completed with last_outcome carrying the truth.
    """
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    captured = _patch_crm(monkeypatch)
    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_hu", outcome="hung_up", notes="answered, paused ~30s, hung up"
    )
    assert result["ok"] is True
    assert captured["call_state"].state == "completed"
    assert captured["call_state"].state != "no_answer"
    assert captured["call_state"].last_outcome == "hung_up"


def test_log_call_rejects_invalid_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    _patch_crm(monkeypatch)
    from backend.crm.log_call import log_call

    result = log_call(prospect_id="pr_x", outcome="maybe", notes="")
    assert result["ok"] is False
    assert "invalid_outcome" in result["error"]


def test_log_call_journals_every_call(tmp_path, monkeypatch):
    """The call is journaled even when the CRM write is degraded."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    _patch_crm(monkeypatch, conv_ok=False, state_ok=False)
    from backend.crm.log_call import _journal_path, log_call

    result = log_call(
        prospect_id="pr_j", company="Journaled Co", outcome="follow_up", notes="call back Friday"
    )
    assert result["ok"] is False  # CRM write degraded
    assert result["journal_persisted"] is True  # journal still captured it

    journal = _journal_path()
    assert journal.exists()
    rec = json.loads(journal.read_text(encoding="utf-8").strip())
    assert rec["prospect_id"] == "pr_j"
    assert rec["outcome"] == "follow_up"
    assert rec["notes"] == "call back Friday"


def test_log_call_booked_opportunity_failure_is_soft(tmp_path, monkeypatch):
    """A degraded Opportunity write does not fail the call log."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    _patch_crm(monkeypatch, opp_status="failed")
    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_b", company="Booked Co", outcome="booked", notes="deal closed on the call"
    )
    assert result["ok"] is True  # conversation + call-state still persisted
    assert result["opportunity_id"] == ""  # opportunity degraded — soft-failed


def test_main_cli_logs_a_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    _patch_crm(monkeypatch)
    from backend.crm.log_call import main

    code = main(["--prospect-id", "pr_cli", "--outcome", "voicemail", "--notes", "left vm"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["outcome"] == "voicemail"
