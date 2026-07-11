"""Smoke tests for the outreach workcell — FSM + metrics + service."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.outreach.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _reset_metrics():
    from backend.outreach import metrics

    metrics.reset_metrics()


def test_fsm_full_path():
    from backend.outreach.fsm import next_state

    assert next_state("open", {}) == "pitch"
    assert next_state("pitch", {}) == "engage"
    assert next_state("engage", {"objection": True}) == "handle_objection"
    assert next_state("engage", {"objection": False}) == "close_attempt"
    assert next_state("handle_objection", {}) == "close_attempt"
    assert next_state("close_attempt", {"resistance": True}) == "fallback"
    assert next_state("close_attempt", {"resistance": False}) == "exit"
    assert next_state("fallback", {}) == "exit"
    assert next_state("exit", {}) == "exit"


def test_decide_action_per_state():
    from backend.outreach.fsm import decide_action
    from backend.outreach.models import OutreachIntel

    intel = OutreachIntel(products={"primary": "seo", "secondary": "ads"})
    assert decide_action("open", intel) == "deliver_opener"
    assert decide_action("pitch", intel) == "deliver_pitch"
    assert decide_action("engage", intel) == "ask_question"
    assert decide_action("close_attempt", intel) == "attempt_close_on_seo"
    assert decide_action("fallback", intel) == "pivot_to_ads"
    assert decide_action("handle_objection", intel, objection_response="re_open") == "re_open"


def test_metrics_log_and_snapshot():
    _reset_metrics()
    from backend.outreach import metrics

    metrics.log_interaction("pr1", "closed", None, "seo", "pain")
    metrics.log_interaction("pr2", "failed", "too expensive", "seo", "pain")
    metrics.log_interaction("pr3", "closed", None, "seo", "value")
    assert dict(metrics.get_top_objections()) == {"too expensive": 1}
    assert dict(metrics.get_best_products()) == {"seo": 2}
    perf = metrics.get_angle_performance()
    assert perf["value"] == 1.0
    assert 0 < perf["pain"] < 1.0


def test_service_advance_call(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach.models import OutreachAdvanceRequest, OutreachIntel
    from backend.outreach.service import advance_call

    req = OutreachAdvanceRequest(
        prospect_id="pr_test",
        current_state="open",
        intel=OutreachIntel(products={"primary": "seo"}),
    )
    step = advance_call(req)
    assert step.current_state == "open"
    assert step.next_state == "pitch"
    assert step.action == "deliver_opener"
    assert step.primary_product == "seo"


def test_service_advance_call_idempotent(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach.models import OutreachAdvanceRequest, OutreachIntel
    from backend.outreach.service import advance_call

    req = OutreachAdvanceRequest(
        prospect_id="pr_x",
        current_state="pitch",
        intel=OutreachIntel(products={"primary": "ads"}),
    )
    a = advance_call(req)
    b = advance_call(req)
    assert a.model_dump() == b.model_dump()
