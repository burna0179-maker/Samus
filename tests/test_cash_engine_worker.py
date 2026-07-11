"""Worker — staged sequence walk, codex halts, dormancy, re-engagement, drain."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.cash_engine import queue as cash_queue
from backend.cash_engine import stages as stages_mod
from backend.cash_engine.stages import StageContext, StageResult
from backend.cash_engine.state import STAGE_SEQUENCE, CashEngineState, load_state, save_state
from backend.cash_engine.worker import drain, process_job, reengage
from backend.crm.models import Opportunity

VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


def _opp(stake=VALID_STAKE, opportunity_id="op-1", prospect_id="pr-1", stage="proposal"):
    return Opportunity(
        opportunity_id=opportunity_id,
        prospect_id=prospect_id,
        stage=stage,
        stake_sentence=stake,
    )


def _prospect(prospect_id="pr-1"):
    # crm.models.Prospect is lenient (extra="ignore"); give the fields the
    # self-contained leaves read. owner_email is mandatory for the
    # outreach-compose stage's cold-sendable gate (OUTREACH_COMPOSE_BLOCKED).
    from backend.crm.models import Prospect

    return Prospect(
        prospect_id=prospect_id,
        company_name="Acme Plumbing",
        website_url="https://acme.test",
        industry="plumbing",
        zipcode="95901",
        phone="555-0100",
        owner_email="founder@acme.test",
    )


class FakeCRM:
    def __init__(self, opp=None, prospect=None, prior_call_state=None):
        self._opp = opp
        self._prospect = prospect
        self._prior = prior_call_state
        self.artifacts = []
        self.call_states = []
        self.operator_tasks = []

    def get_opportunity(self, oid):
        return self._opp

    def get_prospect(self, pid):
        return self._prospect

    def get_call_state(self, pid):
        return self._prior

    def upsert_call_state(self, state):
        self.call_states.append(state)
        return True

    def create_artifact(self, req):
        self.artifacts.append(req)
        return SimpleNamespace(artifact_id=f"art-{len(self.artifacts)}", status="created")

    def create_operator_task(self, req):
        self.operator_tasks.append(req)
        return SimpleNamespace(operator_task_id=f"task-{len(self.operator_tasks)}")


def _job(opportunity_id="op-1", prospect_id="pr-1"):
    return {
        "payload": {
            "opportunity_id": opportunity_id,
            "prospect_id": prospect_id,
            "trigger_source": "manual_review",
            "task_id": "ce-test",
        }
    }


def _ok_handlers(calls):
    def mk(name, detail):
        def h(ctx):
            calls.append(name)
            return StageResult(ok=True, detail=detail)

        return h

    return {
        "audit": mk("audit", {"gap_report_artifact_id": "g1"}),
        "proposal": mk("proposal", {"proposal_ref": "p1"}),
        "contact": mk("contact", {"callsheet_artifact_id": "c1", "voicemail_artifact_id": "v1"}),
        "outreach": mk(
            "outreach", {"outreach_ref": "o1", "outreach_scheduled_for": "2026-06-04T00:00:00Z"}
        ),
        "deliver": mk("deliver", {"website_order_id": "wb1"}),
    }


# --------------------------------------------------------------------------
# Walk
# --------------------------------------------------------------------------


def test_full_traverse_reaches_dormant():
    calls = []
    st = process_job(_job(), handlers=_ok_handlers(calls), crm=FakeCRM(opp=_opp()))
    assert calls == list(STAGE_SEQUENCE)
    assert st.status == "dormant"
    assert st.completed_stages == list(STAGE_SEQUENCE)
    assert st.gap_report_artifact_id == "g1"
    assert st.outreach_ref == "o1"
    assert st.dormant_since != ""


def test_reprocessing_is_idempotent():
    calls = []
    crm = FakeCRM(opp=_opp())
    process_job(_job(), handlers=_ok_handlers(calls), crm=crm)
    assert calls == list(STAGE_SEQUENCE)
    # Second delivery of the same job: no stage handler runs again.
    st2 = process_job(_job(), handlers=_ok_handlers(calls), crm=crm)
    assert calls == list(STAGE_SEQUENCE)  # unchanged
    assert st2.status == "dormant"


def test_codex_block_halts_and_escalates():
    calls = []
    handlers = _ok_handlers(calls)

    def blocked(ctx):
        calls.append("outreach")
        return StageResult(
            ok=False, codex_blocked=True, violated_rule_id="VR-G8", reason="no warmth signal"
        )

    handlers["outreach"] = blocked
    st = process_job(_job(), handlers=handlers, crm=FakeCRM(opp=_opp()))
    assert st.status == "escalated"
    assert st.escalation["stage"] == "outreach"
    assert st.escalation["violated_rule_id"] == "VR-G8"
    assert st.completed_stages == ["audit", "proposal", "contact"]


def test_unwired_stage_parks_and_stops():
    calls = []
    handlers = _ok_handlers(calls)
    handlers["proposal"] = lambda ctx: StageResult(ok=False, parked=True, park_reason="no_route")
    st = process_job(_job(), handlers=handlers, crm=FakeCRM(opp=_opp()))
    assert st.status == "parked"
    assert st.park == {"stage": "proposal", "reason": "no_route"}
    assert st.completed_stages == ["audit"]
    assert "contact" not in calls and "outreach" not in calls


def test_missing_stake_is_refused():
    st = process_job(_job(), handlers=_ok_handlers([]), crm=FakeCRM(opp=_opp(stake="")))
    assert st.status == "escalated"
    assert st.escalation["reason"] == "stake_sentence_missing"


def test_missing_opportunity_drops():
    st = process_job(_job(), handlers=_ok_handlers([]), crm=FakeCRM(opp=None))
    assert st is None


# --------------------------------------------------------------------------
# Default (real) stage handlers
# --------------------------------------------------------------------------


def _ctx(crm, *, state=None, prospect=None, opp=None):
    state = state or CashEngineState(opportunity_id="op-1", prospect_id="pr-1")
    return StageContext(
        state=state,
        opportunity=opp or _opp(),
        prospect=prospect,
        stake_sentence=VALID_STAKE,
        crm=crm,
    )


def test_audit_stage_real_codex_and_artifact(monkeypatch):
    from backend.seo.models import AuditResult

    fake_audit = AuditResult(
        url="https://acme.test",
        seo_score=37,
        issues=[],
        findings={},
        evidence_sources={},
        ts="2026-06-01T00:00:00Z",
    )
    monkeypatch.setattr("backend.seo.audit.audit_url", lambda url, industry="": fake_audit)

    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    res = stages_mod._audit_stage(_ctx(crm, prospect=_prospect()))
    assert res.ok is True
    assert res.detail["gap_report_artifact_id"] == "art-1"
    assert crm.artifacts[0].kind == "seo_audit"


def test_audit_stage_parks_without_website():
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    p = _prospect()
    p.website_url = ""
    res = stages_mod._audit_stage(_ctx(crm, prospect=p))
    assert res.parked is True
    assert res.park_reason == "no_website_url"


def test_contact_stage_drafts_both_artifacts():
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    res = stages_mod._contact_stage(_ctx(crm, prospect=_prospect()))
    assert res.ok is True
    assert res.detail["callsheet_artifact_id"]
    assert res.detail["voicemail_artifact_id"]
    kinds = {a.kind for a in crm.artifacts}
    assert kinds == {"call_sheet", "voicemail"}


def test_proposal_stage_generates_and_records():
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    res = stages_mod._proposal_stage(_ctx(crm, prospect=_prospect()))
    assert res.ok is True
    assert res.detail["proposal_ref"] == "art-1"  # registered in-process
    assert res.detail["proposal_status"] in ("approved", "needs_review", "out_of_scope")
    assert crm.artifacts[0].kind == "proposal"


def test_outreach_stage_composes_drafts_and_schedules():
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    res = stages_mod._outreach_stage(_ctx(crm, prospect=_prospect()))
    assert res.ok is True  # stake + legitimacy clear G1/G8
    assert res.detail["outreach_scheduled_for"]
    assert len(crm.call_states) == 1
    assert crm.call_states[0].state == "outreach_sent"
    # Composed a real draft; no live send by default -> ref is the draft id.
    assert {a.kind for a in crm.artifacts} == {"content_draft"}
    assert res.detail["outreach_ref"] == "art-1"


def test_outreach_stage_indexes_recipient(monkeypatch):
    # When the prospect has an email, the outreach stage records it in the
    # recipient index so a future bounce/complaint resolves back to the deal.
    captured = {}
    monkeypatch.setattr(
        "backend.common.recipient_index.record_recipient",
        lambda **kw: captured.update(kw) or True,
    )
    p = _prospect()
    p.owner_email = "owner@acme.test"
    res = stages_mod._outreach_stage(_ctx(FakeCRM(opp=_opp(), prospect=p), prospect=p))
    assert res.ok is True
    assert captured["email"] == "owner@acme.test"
    assert captured["prospect_id"] == "pr-1"
    assert captured["opportunity_id"] == "op-1"


def test_full_sequence_with_real_handlers_parks_at_deliver_until_won(monkeypatch):
    """End-to-end through the REAL handlers (only the live SEO fetch mocked).

    Proves the chain runs without external infra and without firing live email:
    audit (real, fetch mocked) -> proposal (real, deterministic) -> contact
    (real callsheet+voicemail) -> outreach (real compose+draft+schedule, no
    send) -> deliver. The deal is NOT won (stage='proposal'), so deliver parks
    honestly rather than building a website for an unclosed deal.
    """
    from backend.seo.models import AuditResult

    fake_audit = AuditResult(
        url="https://acme.test",
        seo_score=40,
        issues=[],
        findings={},
        evidence_sources={},
        ts="2026-06-01T00:00:00Z",
    )
    monkeypatch.setattr("backend.seo.audit.audit_url", lambda url, industry="": fake_audit)

    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    st = process_job(_job(), crm=crm)  # handlers=None -> real DEFAULT_HANDLERS
    assert st.status == "parked"
    assert st.park["stage"] == "deliver"
    assert st.park["reason"].startswith("deal_not_won")
    assert st.completed_stages == ["audit", "proposal", "contact", "outreach"]
    assert st.gap_report_artifact_id
    assert st.callsheet_artifact_id and st.voicemail_artifact_id
    assert st.outreach_ref

    kinds = {a.kind for a in crm.artifacts}
    assert {"seo_audit", "proposal", "call_sheet", "voicemail", "content_draft"} <= kinds
    assert crm.call_states[-1].state == "outreach_sent"


def test_deliver_stage_bridges_won_deal_to_website_builder(monkeypatch):
    """A WON deal advances through deliver -> opens a website build -> dormant."""
    from backend.seo.models import AuditResult

    fake_audit = AuditResult(
        url="https://acme.test",
        seo_score=40,
        issues=[],
        findings={},
        evidence_sources={},
        ts="2026-06-01T00:00:00Z",
    )
    monkeypatch.setattr("backend.seo.audit.audit_url", lambda url, industry="": fake_audit)

    # Stub the website bridge so the deliver stage records a real order id
    # without requiring the website builder to be enabled in this offline test.
    import backend.website.service as website_service

    class _Build:
        order_id = "wb-test-1"

    monkeypatch.setattr(
        website_service,
        "from_cash_engine_deal",
        lambda **kw: _Build(),
    )

    crm = FakeCRM(opp=_opp(stage="closed_won"), prospect=_prospect())
    st = process_job(_job(), crm=crm)
    assert st.status == "dormant"
    assert st.completed_stages == list(STAGE_SEQUENCE)
    assert st.website_order_id == "wb-test-1"


def test_deliver_stage_parks_when_builder_disabled(monkeypatch):
    """Won deal but website builder off -> deliver parks honestly (no fake order)."""
    from backend.seo.models import AuditResult

    fake_audit = AuditResult(
        url="https://acme.test",
        seo_score=40,
        issues=[],
        findings={},
        evidence_sources={},
        ts="2026-06-01T00:00:00Z",
    )
    monkeypatch.setattr("backend.seo.audit.audit_url", lambda url, industry="": fake_audit)

    import backend.website.service as website_service

    def _disabled(**kw):
        raise website_service.WebsiteBuilderDisabled("off")

    monkeypatch.setattr(website_service, "from_cash_engine_deal", _disabled)

    crm = FakeCRM(opp=_opp(stage="closed_won"), prospect=_prospect())
    st = process_job(_job(), crm=crm)
    assert st.status == "parked"
    assert st.park["stage"] == "deliver"
    assert st.park["reason"] == "website_builder_disabled"


# --------------------------------------------------------------------------
# Re-engagement + drain
# --------------------------------------------------------------------------


def test_reengage_drafts_voicemail_and_task():
    save_state(
        CashEngineState(
            opportunity_id="op-1",
            prospect_id="pr-1",
            status="dormant",
            gap_report_artifact_id="g1",
        )
    )
    crm = FakeCRM(opp=_opp(), prospect=_prospect())
    out = reengage(opportunity_id="op-1", event="reply", crm=crm)
    assert out["ok"] is True
    assert out["voicemail_artifact_id"]
    assert out["operator_task_id"] == "task-1"
    assert load_state("op-1").status == "running"  # re-opened by the signal


def test_reengage_without_state_is_noop():
    out = reengage(opportunity_id="op-unknown", event="reply", crm=FakeCRM(opp=_opp()))
    assert out["ok"] is False
    assert out["reason"] == "no_state"


def test_drain_processes_enqueued_jobs():
    cash_queue.enqueue_cash_job(
        task_id="ce-1",
        payload={
            "opportunity_id": "op-1",
            "prospect_id": "pr-1",
            "trigger_source": "manual_review",
        },
    )
    summary = drain(handlers=_ok_handlers([]), crm=FakeCRM(opp=_opp()))
    assert summary["processed"] == 1
    assert summary["dormant"] == 1
