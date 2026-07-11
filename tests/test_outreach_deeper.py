"""Deeper outreach coverage — metrics edge cases, FSM branching, app endpoints."""

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


# ---------------------------------------------------------------------------
# metrics module
# ---------------------------------------------------------------------------


def test_metrics_angle_performance_zero_when_no_wins():
    _reset_metrics()
    from backend.outreach import metrics

    metrics.log_interaction("p1", "failed", None, "seo", "pain")
    metrics.log_interaction("p2", "failed", None, "seo", "pain")
    perf = metrics.get_angle_performance()
    assert "pain" in perf
    assert perf["pain"] == 0.0


def test_reset_metrics_clears_all_counters():
    _reset_metrics()
    from backend.outreach import metrics

    metrics.log_interaction("p1", "closed", "expensive", "seo", "value")
    metrics.log_interaction("p2", "failed", "no_time", "ads", "pain")
    assert dict(metrics.get_top_objections())
    assert dict(metrics.get_best_products())
    assert metrics.get_angle_performance()
    metrics.reset_metrics()
    assert dict(metrics.get_top_objections()) == {}
    assert dict(metrics.get_best_products()) == {}
    assert metrics.get_angle_performance() == {}
    snap = metrics.snapshot()
    assert snap["objections"] == {}
    assert snap["closes"] == {}
    assert snap["failures"] == {}
    assert snap["angles"] == {}


def test_log_outcome_returns_snapshot_with_ts(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _reset_metrics()
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach.models import OutreachLogRequest
    from backend.outreach.service import log_outcome

    out = log_outcome(
        OutreachLogRequest(
            prospect_id="p_ts",
            outcome="closed",
            product="seo",
            angle="value",
            objection=None,
        )
    )
    assert out.prospect_id == "p_ts"
    assert out.outcome == "closed"
    assert isinstance(out.ts, str) and out.ts.strip()
    assert out.metrics_snapshot["closes"].get("seo") == 1


def test_get_metrics_returns_pydantic_snapshot(tmp_path, monkeypatch):
    _reset_metrics()
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach import metrics
    from backend.outreach.models import OutreachMetricsSnapshot
    from backend.outreach.service import get_metrics

    metrics.log_interaction("p1", "closed", None, "seo", "value")
    metrics.log_interaction("p2", "failed", "too_expensive", "ads", "pain")
    snap = get_metrics()
    assert isinstance(snap, OutreachMetricsSnapshot)
    assert hasattr(snap, "top_objections")
    assert hasattr(snap, "best_products")
    assert hasattr(snap, "angle_performance")
    assert ("too_expensive", 1) in snap.top_objections
    assert ("seo", 1) in snap.best_products
    assert snap.angle_performance["value"] == 1.0


# ---------------------------------------------------------------------------
# FSM / service branching
# ---------------------------------------------------------------------------


def test_advance_call_with_objection_routes_handler(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach.models import OutreachAdvanceRequest, OutreachIntel
    from backend.outreach.service import advance_call

    req = OutreachAdvanceRequest(
        prospect_id="p_obj",
        current_state="handle_objection",
        objection_detected=True,
        objection_response="empathic_listen",
        intel=OutreachIntel(products={"primary": "seo", "secondary": "ads"}),
    )
    step = advance_call(req)
    assert step.action == "empathic_listen"
    assert step.next_state == "close_attempt"


def test_advance_call_with_resistance_falls_back(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach.models import OutreachAdvanceRequest, OutreachIntel
    from backend.outreach.service import advance_call

    req = OutreachAdvanceRequest(
        prospect_id="p_resist",
        current_state="close_attempt",
        user_input="not interested",
        intel=OutreachIntel(products={"primary": "seo", "secondary": "ads"}),
    )
    step = advance_call(req)
    assert step.next_state == "fallback"
    assert step.action == "attempt_close_on_seo"


def test_advance_call_intel_missing_secondary_product(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.outreach.models import OutreachAdvanceRequest, OutreachIntel
    from backend.outreach.service import advance_call

    req = OutreachAdvanceRequest(
        prospect_id="p_nosec",
        current_state="fallback",
        intel=OutreachIntel(products={"primary": "seo"}),
    )
    step = advance_call(req)
    assert step.action == "offer_free_audit"


def test_send_message_sms_still_raises_not_implemented():
    import pytest
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_x",
        channel="sms",
        template_id="tmpl_x",
        body="hi",
    )
    with pytest.raises(NotImplementedError):
        send_message(req)


def test_send_message_call_degraded_when_voice_send_disabled(tmp_path, monkeypatch):
    """Default OFF: call channel returns a degraded receipt, never fires Vapi."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("SAMUS_OUTREACH_VOICE_SEND", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_call",
        channel="call",
        template_id="t",
        phone="+15551234567",
    )
    out = send_message(req)
    assert out["status"] == "degraded"
    assert out["voice_skip_reason"] == "outreach_voice_send_disabled"
    assert out["message_id"] == ""


def test_send_message_call_degraded_when_assistant_unset(tmp_path, monkeypatch):
    """Armed but no assistant/phone configured -> fail-closed degraded."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_OUTREACH_VOICE_SEND", "1")
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    monkeypatch.delenv("VAPI_PHONE_NUMBER_ID", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_call2",
        channel="call",
        template_id="t",
        phone="+15551234567",
    )
    out = send_message(req)
    assert out["status"] == "degraded"
    assert out["voice_skip_reason"] == "vapi_assistant_or_phone_unset"


def test_send_message_call_delegates_to_voice_workcell(tmp_path, monkeypatch):
    """Armed + configured -> delegates to the real Vapi path (voice workcell)."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_OUTREACH_VOICE_SEND", "1")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_1")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "phone_1")
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.voice import service as voice_service
    from backend.voice.models import InitiateCallResult

    captured = {}

    def _fake_initiate(req):
        captured["assistant_id"] = req.assistant_id
        captured["phone_number_id"] = req.phone_number_id
        captured["customer_number"] = req.customer_number
        return InitiateCallResult(call_id="call_abc", status="queued", vapi_error=None)

    monkeypatch.setattr(voice_service, "initiate_call", _fake_initiate)
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_call3",
        channel="call",
        template_id="t",
        phone="+15551234567",
        company="Acme",
    )
    out = send_message(req)
    assert out["message_id"] == "call_abc"
    assert out["status"] == "queued"
    assert captured["assistant_id"] == "asst_1"
    assert captured["customer_number"] == "+15551234567"


# ---------------------------------------------------------------------------
# app endpoints (TestClient)
# ---------------------------------------------------------------------------


def test_app_advance_endpoint_returns_outreach_step(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.outreach.app import app

    client = TestClient(app)
    r = client.post(
        "/advance",
        json={
            "prospect_id": "p_app",
            "current_state": "open",
            "user_input": "",
            "intel": {"products": {"primary": "seo"}, "signals": []},
            "objection_detected": False,
            "objection_response": None,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_state"] == "pitch"
    assert body["action"] == "deliver_opener"
    assert body["primary_product"] == "seo"


def test_app_outcome_endpoint_writes_metric(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _reset_metrics()
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.outreach.app import app

    client = TestClient(app)
    r = client.post(
        "/outcome",
        json={
            "prospect_id": "p_metric",
            "outcome": "closed",
            "product": "seo",
            "angle": "value",
            "objection": None,
        },
    )
    assert r.status_code == 200, r.text

    snap_r = client.get("/metrics_snapshot")
    assert snap_r.status_code == 200, snap_r.text
    body = snap_r.json()
    pairs = [(p, c) for p, c in body["best_products"]]
    assert ("seo", 1) in pairs
    assert body["angle_performance"]["value"] == 1.0


def test_metrics_snapshot_endpoint_standalone_on_fresh_state():
    """GET /metrics_snapshot is a standalone observability endpoint: it serves
    the documented OutreachMetricsSnapshot shape independently of any prior
    log_outcome write — confirming it is not a dead route but a deliberate
    operator-poll surface (cf. finance /snapshot, crm /feedback/snapshot)."""
    _reset_metrics()
    from fastapi.testclient import TestClient
    from backend.outreach.app import app

    client = TestClient(app)

    r = client.get("/metrics_snapshot")
    assert r.status_code == 200, r.text
    body = r.json()
    # Documented OutreachMetricsSnapshot fields are always present, even empty.
    assert set(body) == {"top_objections", "best_products", "angle_performance"}
    assert body["top_objections"] == []
    assert body["best_products"] == []
    assert body["angle_performance"] == {}
