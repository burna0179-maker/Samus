"""Cash-engine decision records — revenue-blocking gate/park/escalate reach the
unified decision spine.

The cash engine's loop-terminating decisions (front-door admit/reject, and the
walk's escalate/park/complete) decide whether a prospect advances toward
revenue or rests. They were previously observable ONLY as a per-opportunity
``state.log`` line + review ledger — never on the ``decision.made`` spine that
planner + arbiter use. These tests assert each now mints a reconstructable
DecisionRecord (actor, why, risk, journey correlation) additively, without
changing the state machine, and fail-soft.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.cash_engine.models import RevenueTriggerRequest
from backend.cash_engine.service import review_opportunity
from backend.cash_engine.stages import StageResult
from backend.cash_engine.worker import process_job
from backend.common.decision_record import list_decisions
from backend.crm.models import Opportunity

VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "business_events.jsonl"),
    )
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")


# --- worker fixtures -----------------------------------------------------

def _opp(stake=VALID_STAKE, opportunity_id="op-1", prospect_id="pr-1", stage="proposal"):
    return Opportunity(
        opportunity_id=opportunity_id, prospect_id=prospect_id,
        stage=stage, stake_sentence=stake,
    )


class _WorkerCRM:
    def __init__(self, opp=None):
        self._opp = opp

    def get_opportunity(self, oid):
        return self._opp

    def get_prospect(self, pid):
        return None


def _job(opportunity_id="op-1", prospect_id="pr-1"):
    return {"payload": {
        "opportunity_id": opportunity_id, "prospect_id": prospect_id,
        "trigger_source": "manual_review", "task_id": "ce-test",
    }}


def _ok_handlers():
    def mk(detail):
        return lambda ctx: StageResult(ok=True, detail=detail)
    return {
        "audit": mk({"gap_report_artifact_id": "g1"}),
        "proposal": mk({"proposal_ref": "p1"}),
        "contact": mk({"callsheet_artifact_id": "c1", "voicemail_artifact_id": "v1"}),
        "outreach": mk({"outreach_ref": "o1", "outreach_scheduled_for": "2026-06-04T00:00:00Z"}),
        "deliver": mk({"website_order_id": "wb1"}),
    }


def _decisions(opportunity_id="op-1", actor=None):
    return list_decisions(opportunity_id=opportunity_id, actor=actor, limit=50)


# --- service (front-door) fixtures --------------------------------------

class _GateCRM:
    def __init__(self, opp=None):
        self._opp = opp

    def get_opportunity_for_prospect(self, prospect_id):
        return self._opp

    def get_call_state(self, prospect_id):
        return None


def _req(**kw):
    return RevenueTriggerRequest(
        prospect_id=kw.get("prospect_id", "pr-1"),
        trigger_source=kw.get("trigger_source", "manual_review"),
        current_samus_state=kw.get("current_samus_state", ""),
    )


# ==========================================================================
# Worker walk — escalate / park / complete
# ==========================================================================

class TestWalkDecisions:
    def test_codex_block_emits_escalate_decision(self):
        handlers = _ok_handlers()
        handlers["outreach"] = lambda ctx: StageResult(
            ok=False, codex_blocked=True,
            violated_rule_id="VR-G8", reason="no warmth signal",
        )
        process_job(_job(), handlers=handlers, crm=_WorkerCRM(opp=_opp()))

        recs = _decisions(actor="cash_engine_worker")
        assert len(recs) == 1
        rec = recs[0]
        assert rec["extra"]["decision_kind"] == "escalate"
        assert rec["extra"]["stage"] == "outreach"
        assert rec["risk_level"] == "high"
        assert rec["why"] == "no warmth signal"
        assert rec["opportunity_id"] == "op-1"
        assert "VR-G8" in " ".join(rec["data_used"])

    def test_park_emits_park_decision(self):
        handlers = _ok_handlers()
        handlers["proposal"] = lambda ctx: StageResult(
            ok=False, parked=True, park_reason="no_route",
        )
        process_job(_job(), handlers=handlers, crm=_WorkerCRM(opp=_opp()))

        recs = _decisions(actor="cash_engine_worker")
        assert len(recs) == 1
        assert recs[0]["extra"]["decision_kind"] == "park"
        assert recs[0]["extra"]["stage"] == "proposal"
        assert recs[0]["why"] == "no_route"
        assert recs[0]["risk_level"] == "normal"

    def test_full_walk_emits_single_complete_decision(self):
        process_job(_job(), handlers=_ok_handlers(), crm=_WorkerCRM(opp=_opp()))
        recs = _decisions(actor="cash_engine_worker")
        assert len(recs) == 1
        assert recs[0]["extra"]["decision_kind"] == "complete"

    def test_stake_missing_emits_escalate_decision(self):
        process_job(_job(), handlers=_ok_handlers(), crm=_WorkerCRM(opp=_opp(stake="")))
        recs = _decisions(actor="cash_engine_worker")
        assert len(recs) == 1
        assert recs[0]["extra"]["decision_kind"] == "escalate"
        assert recs[0]["why"] == "stake_sentence_missing"
        assert recs[0]["risk_level"] == "high"

    def test_no_handler_emits_park_decision(self):
        handlers = _ok_handlers()
        del handlers["audit"]  # first stage unwired -> no_handler park
        process_job(_job(), handlers=handlers, crm=_WorkerCRM(opp=_opp()))
        recs = _decisions(actor="cash_engine_worker")
        assert len(recs) == 1
        assert recs[0]["extra"]["decision_kind"] == "park"
        assert recs[0]["why"] == "no_handler"

    def test_decision_emit_is_failsoft(self, monkeypatch):
        """A telemetry fault must NOT break the walk's state machine."""
        import backend.cash_engine.worker as worker_mod

        def _boom(*a, **k):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr(worker_mod, "record_decision", _boom)
        handlers = _ok_handlers()
        handlers["outreach"] = lambda ctx: StageResult(
            ok=False, codex_blocked=True, violated_rule_id="VR-G8", reason="x",
        )
        st = process_job(_job(), handlers=handlers, crm=_WorkerCRM(opp=_opp()))
        # Walk still escalated correctly despite the telemetry fault.
        assert st.status == "escalated"
        assert st.escalation["violated_rule_id"] == "VR-G8"


# ==========================================================================
# Front door — admit / reject
# ==========================================================================

class TestFrontDoorDecisions:
    def test_clean_gate_emits_admitted_decision(self):
        review_opportunity(
            _req(), crm=_GateCRM(opp=_opp()),
            enqueue=lambda **k: {"queue": "mock:test", "task_id": k["task_id"]},
        )
        recs = _decisions(actor="cash_engine_gate")
        assert len(recs) == 1
        assert recs[0]["extra"]["decision_kind"] == "front_door_enqueued"
        assert recs[0]["risk_level"] == "normal"
        assert recs[0]["opportunity_id"] == "op-1"

    def test_blocked_gate_emits_escalate_decision(self):
        review_opportunity(
            _req(), crm=_GateCRM(opp=_opp(stake="")),
            enqueue=lambda **k: None,
        )
        recs = _decisions(actor="cash_engine_gate")
        assert len(recs) == 1
        assert recs[0]["extra"]["decision_kind"] == "front_door_escalated"
        assert recs[0]["risk_level"] == "high"
        assert recs[0]["extra"]["required_protocol"] == "stake_sentence"

    def test_front_door_failsoft(self, monkeypatch):
        import backend.cash_engine.service as service_mod

        def _boom(*a, **k):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr(service_mod, "record_decision", _boom)
        res = review_opportunity(
            _req(), crm=_GateCRM(opp=_opp()),
            enqueue=lambda **k: {"queue": "mock:test", "task_id": k["task_id"]},
        )
        # Front door still admitted despite the telemetry fault.
        assert res.accepted is True
        assert res.status == "enqueued"
