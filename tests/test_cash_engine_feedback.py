"""Feedback -> cash-engine re-engagement bridge + the engagement webhook."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.cash_engine.state import CashEngineState, load_state, save_state
from backend.feedback.handlers import fire_cash_engine_signal

VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


def _opp(stake=VALID_STAKE, opportunity_id="op-1", prospect_id="pr-1"):
    from backend.crm.models import Opportunity
    return Opportunity(
        opportunity_id=opportunity_id, prospect_id=prospect_id,
        stage="proposal", stake_sentence=stake,
    )


def _prospect(prospect_id="pr-1"):
    from backend.crm.models import Prospect
    return Prospect(prospect_id=prospect_id, company_name="Acme Plumbing")


class FakeCRM:
    def __init__(self, opp=None, prospect=None):
        self._opp = opp
        self._prospect = prospect
        self.artifacts = []
        self.operator_tasks = []

    def get_opportunity(self, oid):
        return self._opp

    def get_opportunity_for_prospect(self, pid):
        return self._opp

    def get_prospect(self, pid):
        return self._prospect

    def create_artifact(self, req):
        self.artifacts.append(req)
        return SimpleNamespace(artifact_id=f"art-{len(self.artifacts)}", status="created")

    def create_operator_task(self, req):
        self.operator_tasks.append(req)
        return SimpleNamespace(operator_task_id=f"task-{len(self.operator_tasks)}")


def _dormant_state(opportunity_id="op-1", prospect_id="pr-1"):
    save_state(CashEngineState(
        opportunity_id=opportunity_id, prospect_id=prospect_id,
        status="dormant", gap_report_artifact_id="g1",
    ))


def test_positive_reply_reengages_dormant_deal():
    _dormant_state()
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    out = fire_cash_engine_signal(event="reply", opportunity_id="op-1", crm=crm)
    assert out["ok"] is True
    assert out["event"] == "reply"
    assert out["voicemail_artifact_id"]
    assert load_state("op-1").status == "running"   # re-opened


def test_open_resolves_via_prospect_id():
    _dormant_state()
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    out = fire_cash_engine_signal(event="opened", prospect_id="pr-1", crm=crm)
    assert out["ok"] is True
    assert out["event"] == "opened"


def test_negative_bounce_halts_deal():
    _dormant_state()
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    out = fire_cash_engine_signal(event="bounce", opportunity_id="op-1", crm=crm)
    assert out["ok"] is True
    assert out["status"] == "halted"
    assert load_state("op-1").status == "halted"


def test_reengage_skipped_when_not_dormant():
    # A deal still mid-walk must not be re-engaged by an open.
    save_state(CashEngineState(opportunity_id="op-1", prospect_id="pr-1", status="running"))
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    out = fire_cash_engine_signal(event="open", opportunity_id="op-1", crm=crm)
    assert out["ok"] is False
    assert out["reason"].startswith("not_dormant")


def test_unknown_event_is_rejected():
    _dormant_state()
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    out = fire_cash_engine_signal(event="laughed", opportunity_id="op-1", crm=crm)
    assert out["ok"] is False
    assert out["reason"].startswith("unknown_event")


def test_no_opportunity_resolution_is_a_clean_noop():
    crm = FakeCRM(opp=None, prospect=None)
    out = fire_cash_engine_signal(event="reply", prospect_id="pr-unknown", crm=crm)
    assert out["ok"] is False
    assert out["reason"] == "no_opportunity"


def test_bounce_halts_deal_via_recipient_index(monkeypatch):
    # A bounced address resolves through the index back to op-1, which halts.
    _dormant_state()
    monkeypatch.setattr(
        "backend.common.recipient_index.lookup_recipient",
        lambda email, **kw: {"prospect_id": "pr-1", "opportunity_id": "op-1"},
    )
    from backend.feedback.handlers import _halt_cash_engine_for_emails
    _halt_cash_engine_for_emails(["bounced@acme.test"], "bounce")
    assert load_state("op-1").status == "halted"


def test_unknown_bounce_address_is_noop(monkeypatch):
    _dormant_state()
    monkeypatch.setattr(
        "backend.common.recipient_index.lookup_recipient", lambda email, **kw: None,
    )
    from backend.feedback.handlers import _halt_cash_engine_for_emails
    _halt_cash_engine_for_emails(["stranger@nowhere.test"], "bounce")
    assert load_state("op-1").status == "dormant"   # untouched


def test_disabled_engine_gates_the_signal(monkeypatch):
    monkeypatch.setenv("SAMUS_CASH_ENGINE_ENABLED", "false")
    from backend.common.settings import reload_settings
    reload_settings()
    out = fire_cash_engine_signal(event="reply", opportunity_id="op-1", crm=FakeCRM(opp=_opp()))
    assert out["ok"] is False
    assert out["reason"] == "cash_engine_disabled"


# --------------------------------------------------------------------------
# Webhook route
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_FEEDBACK_VERIFY_SNS", "0")
    from backend.common.settings import reload_settings
    reload_settings()
    from fastapi.testclient import TestClient
    from backend.feedback.app import app
    return TestClient(app)


def test_webhook_requires_event(client):
    resp = client.post("/api/feedback/engagement", json={})
    assert resp.status_code == 422


def test_webhook_returns_structured_verdict(client):
    # No CRM row / state -> a clean best-effort no-op, still 200.
    resp = client.post(
        "/api/feedback/engagement",
        json={"event": "reply", "prospect_id": "pr-unknown"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] in ("no_opportunity", "no_state", "resolve_failed")
