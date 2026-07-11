"""review_opportunity orchestration + the portfolio_controller decay sweep."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.cash_engine.models import RevenueTriggerRequest, RevenueTriggerResult
from backend.cash_engine.service import review_opportunity
from backend.crm.models import Opportunity
from backend.portfolio_controller.decay_scan import scan_for_decay_triggers

VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
FMT = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    # Redirect the cash_engine JSONL ledger/mock-queue under a tmp root so the
    # review path never writes into the repo working tree.
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


class FakeCRM:
    def __init__(self, opp=None, call_state=None, opps=None):
        self._opp = opp
        self._call_state = call_state
        self._opps = opps or []

    def get_opportunity_for_prospect(self, prospect_id):
        return self._opp

    def get_call_state(self, prospect_id):
        return self._call_state

    def list_opportunities(self, *, limit=50):
        return SimpleNamespace(opportunities=list(self._opps))


def _opp(stake=VALID_STAKE, stage="proposal", age_days=30, opportunity_id="op-1", prospect_id="pr-1"):
    stamp = (NOW - timedelta(days=age_days)).strftime(FMT)
    return Opportunity(
        opportunity_id=opportunity_id,
        prospect_id=prospect_id,
        stage=stage,
        stake_sentence=stake,
        created_at=stamp,
        updated_at=stamp,
    )


def _req(**kw):
    return RevenueTriggerRequest(
        prospect_id=kw.get("prospect_id", "pr-1"),
        trigger_source=kw.get("trigger_source", "manual_review"),
        current_samus_state=kw.get("current_samus_state", ""),
    )


# --------------------------------------------------------------------------
# review_opportunity
# --------------------------------------------------------------------------

def test_clean_gate_enqueues_with_audit_stage():
    calls = []

    def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return {"queue": "mock:test", "queued": True, "task_id": kwargs["task_id"]}

    res = review_opportunity(_req(), crm=FakeCRM(opp=_opp()), enqueue=fake_enqueue)
    assert res.accepted is True
    assert res.status == "enqueued"
    assert res.task_id.startswith("ce-")
    assert res.queue == "mock:test"
    assert res.opportunity_id == "op-1"
    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert payload["stage"] == "audit"
    assert payload["stake_present"] is True
    assert payload["prospect_id"] == "pr-1"
    assert calls[0]["metadata"]["action"] == "cash_engine_step"


def test_missing_stake_escalates_and_does_not_enqueue():
    calls = []
    res = review_opportunity(
        _req(), crm=FakeCRM(opp=_opp(stake="")),
        enqueue=lambda **k: calls.append(k),
    )
    assert res.accepted is False
    assert res.status == "escalated"
    assert res.required_protocol == "stake_sentence"
    assert calls == []


def test_no_opportunity_is_invalid():
    res = review_opportunity(_req(), crm=FakeCRM(opp=None), enqueue=lambda **k: None)
    assert res.accepted is False
    assert res.status == "invalid"
    assert res.required_protocol == "opportunity"


def test_disabled_engine_parks(monkeypatch):
    monkeypatch.setenv("SAMUS_CASH_ENGINE_ENABLED", "false")
    from backend.common.settings import reload_settings
    reload_settings()

    res = review_opportunity(_req(), crm=FakeCRM(opp=_opp()), enqueue=lambda **k: None)
    assert res.accepted is False
    assert res.status == "disabled"


def test_decay_risk_threads_into_result():
    res = review_opportunity(
        _req(), crm=FakeCRM(opp=_opp()),
        enqueue=lambda **k: {"queue": "mock:test", "task_id": k["task_id"]},
        decay_risk=0.91,
    )
    assert res.decay_risk == 0.91


# --------------------------------------------------------------------------
# decay_scan (signal_decay trigger)
# --------------------------------------------------------------------------

def test_decay_scan_fires_only_on_crossing_deals():
    opps = [
        _opp(stage="proposal", age_days=30, opportunity_id="op-hot", prospect_id="pr-hot"),
        _opp(stage="closed_won", age_days=30, opportunity_id="op-done", prospect_id="pr-done"),
        _opp(stage="new", age_days=0, opportunity_id="op-fresh", prospect_id="pr-fresh"),
    ]
    seen = []

    def fake_review(req, *, crm=None, decay_risk=None):
        seen.append((req.prospect_id, req.trigger_source, decay_risk))
        return RevenueTriggerResult(
            accepted=True, status="enqueued", prospect_id=req.prospect_id,
        )

    out = scan_for_decay_triggers(
        crm=FakeCRM(opps=opps), review=fake_review,
        now=NOW, threshold=0.6, stall_days=7,
    )
    assert out.scanned == 3
    assert out.crossed == 1
    assert out.enqueued == 1
    assert len(seen) == 1
    assert seen[0][0] == "pr-hot"
    assert seen[0][1] == "signal_decay"
    assert seen[0][2] == 1.0


def test_decay_scan_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("SAMUS_CASH_ENGINE_ENABLED", "false")
    from backend.common.settings import reload_settings
    reload_settings()

    called = []
    out = scan_for_decay_triggers(
        crm=FakeCRM(opps=[_opp()]),
        review=lambda *a, **k: called.append(1),
        now=NOW,
    )
    assert out.scanned == 0
    assert called == []
