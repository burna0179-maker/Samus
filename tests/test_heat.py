"""Heat Field tests — scoring, bands, store, service, signature, webhook route."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from backend.heat import controller, metrics
from backend.heat import service as heat_service
from backend.heat import store as heat_store


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_zero_inputs_score_zero():
    assert metrics.compute_heat_score(metrics.HeatInputs()) == 0.0


def test_complaint_alone_can_reach_critical():
    # The nuclear signal: an all-complaints day must trip critical even with
    # zero bounces/blocks (the sum-to-1 model wrongly capped this at "warm").
    score = metrics.compute_heat_score(metrics.HeatInputs(complaint_rate=1.0))
    assert controller.band_for_score(score) == controller.BAND_CRITICAL


def test_realistic_complaint_emergency_is_hot_or_worse():
    # 0.3% sustained complaints is a real reputation emergency.
    score = metrics.compute_heat_score(metrics.HeatInputs(complaint_rate=0.003))
    assert score >= controller.HOT_THRESHOLD


def test_complaint_dominates_bounce():
    complaint = metrics.compute_heat_score(metrics.HeatInputs(complaint_rate=0.01))
    bounce = metrics.compute_heat_score(metrics.HeatInputs(bounce_rate=0.01))
    assert complaint > bounce  # complaint amplified + weighted heaviest


def test_score_clamped_to_one():
    hot = metrics.HeatInputs(complaint_rate=1.0, bounce_rate=1.0, block_rate=1.0, deferral_rate=1.0)
    assert metrics.compute_heat_score(hot) == 1.0


def test_small_complaint_rate_registers():
    # 0.2% complaint rate should already produce meaningful heat.
    score = metrics.compute_heat_score(metrics.HeatInputs(complaint_rate=0.002))
    assert score > 0.1


# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,band",
    [
        (0.0, controller.BAND_COOL),
        (0.24, controller.BAND_COOL),
        (0.25, controller.BAND_WARM),
        (0.49, controller.BAND_WARM),
        (0.50, controller.BAND_HOT),
        (0.74, controller.BAND_HOT),
        (0.75, controller.BAND_CRITICAL),
        (1.0, controller.BAND_CRITICAL),
    ],
)
def test_band_for_score(score, band):
    assert controller.band_for_score(score) == band


def test_multiplier_monotonic_decreasing():
    m = [
        controller.send_multiplier(b)
        for b in (
            controller.BAND_COOL,
            controller.BAND_WARM,
            controller.BAND_HOT,
            controller.BAND_CRITICAL,
        )
    ]
    assert m == sorted(m, reverse=True)
    assert m[0] == 1.0 and m[-1] == 0.0


def test_only_critical_pauses():
    assert controller.is_send_paused(controller.BAND_CRITICAL) is True
    assert controller.is_send_paused(controller.BAND_HOT) is False


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _store(tmp_path, now=None):
    return heat_store.HeatStateStore(
        ddb_table=None,
        json_path=str(tmp_path / "heat.json"),
        now_func=now,
    )


def test_record_send_and_event(tmp_path):
    s = _store(tmp_path)
    s.record_send(3)
    assert s.record_event("delivered") == "delivered"
    assert s.record_event("bounce") == "bounced"
    assert s.record_event("spamreport") == "complained"
    assert s.record_event("totally_unknown") is None
    snap = s.snapshot()
    assert snap.sent == 3 and snap.delivered == 1
    assert snap.bounced == 1 and snap.complained == 1


def test_bucket_resets_on_new_day(tmp_path):
    clock = {"t": 1_700_000_000.0}  # fixed
    s = _store(tmp_path, now=lambda: clock["t"])
    s.record_send(5)
    assert s.snapshot().sent == 5
    clock["t"] += 86400 * 2  # +2 days
    assert s.snapshot().sent == 0  # reset


def test_min_sample_floor_suppresses_noise(tmp_path):
    s = _store(tmp_path)
    s.record_send(3)
    s.record_event("bounce")  # 1 bounce of 3 = 33% but below min-sample
    inputs = s.snapshot().to_inputs()
    assert inputs.bounce_rate == 0.0  # floored — too few sends to act


def test_rates_compute_above_min_sample(tmp_path):
    s = _store(tmp_path)
    for _ in range(heat_store.MIN_SAMPLE_FOR_HEAT):
        s.record_event("delivered")
    s.record_event("bounce")
    inputs = s.snapshot().to_inputs()
    assert inputs.bounce_rate > 0.0


# ---------------------------------------------------------------------------
# service — flag gating + ingest
# ---------------------------------------------------------------------------


@pytest.fixture
def heat_env(tmp_path, monkeypatch):
    """Point the singleton store at a tmp JSON file, no DDB."""
    monkeypatch.setenv("DDB_HEAT_STATE_TABLE", "")
    monkeypatch.setenv("SAMUS_HEAT_STATE_PATH", str(tmp_path / "heat.json"))
    heat_store.reset_store()
    yield
    heat_store.reset_store()


def _set_flag(monkeypatch, enabled: bool):
    monkeypatch.setenv("SAMUS_HEAT_FIELD_ENABLED", "true" if enabled else "false")
    from backend.common.settings import reload_settings

    reload_settings()


def test_multiplier_is_noop_when_disabled(heat_env, monkeypatch):
    _set_flag(monkeypatch, False)
    # Force a critical band by stuffing the store with complaints.
    s = heat_store.get_store()
    for _ in range(heat_store.MIN_SAMPLE_FOR_HEAT):
        s.record_event("delivered")
    for _ in range(heat_store.MIN_SAMPLE_FOR_HEAT):
        s.record_event("spamreport")
    assert heat_service.current_band() == controller.BAND_CRITICAL  # observed
    assert heat_service.send_multiplier_now() == 1.0  # but throttle is dormant
    assert heat_service.is_send_paused_now() is False


def test_throttle_active_when_enabled(heat_env, monkeypatch):
    _set_flag(monkeypatch, True)
    s = heat_store.get_store()
    for _ in range(heat_store.MIN_SAMPLE_FOR_HEAT):
        s.record_event("delivered")
    for _ in range(heat_store.MIN_SAMPLE_FOR_HEAT):
        s.record_event("spamreport")
    assert heat_service.is_send_paused_now() is True
    assert heat_service.send_multiplier_now() == 0.0


def test_ingest_counts_and_halts_and_suppresses(heat_env, monkeypatch):
    _set_flag(monkeypatch, False)
    halts: list = []
    sup: list = []
    monkeypatch.setattr(
        "backend.feedback.handlers.fire_cash_engine_signal",
        lambda **kw: halts.append(kw) or {"ok": True},
    )
    monkeypatch.setattr(
        "backend.common.recipient_index.lookup_recipient",
        lambda email, **kw: {"prospect_id": "pr_1", "opportunity_id": "op_1"},
    )

    class _Tbl:
        def put_item(self, Item=None):  # noqa: N803
            sup.append(Item)

    monkeypatch.setattr("backend.common.aws.table", lambda *a, **k: _Tbl())

    out = heat_service.ingest_sendgrid_events(
        [
            {"event": "delivered", "email": "a@x.com"},
            {"event": "bounce", "email": "bad@x.com"},
            {"event": "spamreport", "email": "angry@x.com", "prospect_id": "pr_9"},
        ]
    )
    assert out["ok"] is True
    assert out["counted"] == 3
    assert out["halted"] == 2  # bounce + spamreport
    assert out["suppressed"] == 2  # both suppressed
    assert len(halts) == 2
    # spamreport mapped to the "complaint" vocabulary for the halt loop.
    assert any(h.get("event") == "complaint" for h in halts)
    assert any(h.get("event") == "bounce" for h in halts)
    snap = heat_store.get_store().snapshot()
    assert snap.delivered == 1 and snap.bounced == 1 and snap.complained == 1


def test_status_snapshot_shape(heat_env, monkeypatch):
    _set_flag(monkeypatch, False)
    heat_service.record_send(2)
    snap = heat_service.status_snapshot()
    assert snap["enabled"] is False
    assert snap["band"] == "cool"
    assert snap["counters"]["sent"] == 2


# ---------------------------------------------------------------------------
# sendgrid signature
# ---------------------------------------------------------------------------


def _gen_ec_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, base64.b64encode(pub_der).decode("ascii")


def test_signature_roundtrip_valid():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from backend.heat.sendgrid_signature import verify_sendgrid_signature

    priv, pub_b64 = _gen_ec_keypair()
    payload = b'[{"event":"delivered","email":"a@x.com"}]'
    ts = "1700000000"
    sig = priv.sign(ts.encode() + payload, ec.ECDSA(hashes.SHA256()))
    # Should not raise.
    verify_sendgrid_signature(
        public_key_b64=pub_b64,
        payload=payload,
        signature_b64=base64.b64encode(sig).decode(),
        timestamp=ts,
    )


def test_signature_tampered_payload_rejected():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from backend.heat.sendgrid_signature import (
        SendGridSignatureError,
        verify_sendgrid_signature,
    )

    priv, pub_b64 = _gen_ec_keypair()
    ts = "1700000000"
    sig = priv.sign(ts.encode() + b"original", ec.ECDSA(hashes.SHA256()))
    with pytest.raises(SendGridSignatureError):
        verify_sendgrid_signature(
            public_key_b64=pub_b64,
            payload=b"TAMPERED",
            signature_b64=base64.b64encode(sig).decode(),
            timestamp=ts,
        )


def test_signature_no_key_raises():
    from backend.heat.sendgrid_signature import (
        SendGridSignatureError,
        verify_sendgrid_signature,
    )

    with pytest.raises(SendGridSignatureError):
        verify_sendgrid_signature(
            public_key_b64="",
            payload=b"x",
            signature_b64="y",
            timestamp="1",
        )


# ---------------------------------------------------------------------------
# webhook route (TestClient)
# ---------------------------------------------------------------------------


def test_webhook_dev_unverified_ingests(heat_env, monkeypatch):
    # Dev posture: verification off -> route accepts + ingests.
    monkeypatch.setenv("SAMUS_SENDGRID_VERIFY_EVENTS", "0")
    monkeypatch.setenv("SAMUS_ENV", "development")
    from backend.common.settings import reload_settings

    reload_settings()
    _set_flag(monkeypatch, False)

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    resp = client.post(
        "/api/sendgrid/events",
        json=[
            {"event": "delivered", "email": "a@x.com"},
            {"event": "open", "email": "a@x.com"},
        ],
    )
    assert resp.status_code == 200
    assert resp.json()["counted"] == 2
    assert heat_store.get_store().snapshot().delivered == 1


def test_webhook_prod_without_key_fails_closed(heat_env, monkeypatch):
    monkeypatch.setenv("SAMUS_SENDGRID_VERIFY_EVENTS", "1")
    monkeypatch.setenv("SENDGRID_WEBHOOK_VERIFICATION_KEY", "")
    monkeypatch.setenv("SAMUS_ENV", "production")
    from backend.common.settings import reload_settings

    reload_settings()

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/sendgrid/events", json=[{"event": "delivered", "email": "a@x.com"}])
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# custom_args plumbing
# ---------------------------------------------------------------------------


def _capture_sendgrid(monkeypatch) -> dict:
    cap: dict = {}

    def _fake(**kw: Any) -> dict[str, str]:
        cap.update(kw)
        return {"message_id": "x", "channel": "email", "to": kw.get("to", ""), "ts": "t"}

    import backend.common.email_backend as adapter

    monkeypatch.setattr(adapter, "send_email_via_sendgrid", _fake)
    return cap


def test_send_email_threads_custom_args(monkeypatch):
    monkeypatch.setenv("EMAIL_BACKEND", "sendgrid")
    monkeypatch.setenv("SAMUS_COMPLIANCE_GUARD_MODE", "off")
    from backend.common.settings import reload_settings

    reload_settings()
    cap = _capture_sendgrid(monkeypatch)
    from backend.common.email_backend import send_email

    send_email("a@b.com", "s", "b", custom_args={"prospect_id": "pr_1"})
    assert cap["custom_args"] == {"prospect_id": "pr_1"}


def test_sendgrid_payload_includes_custom_args(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "samus@example.com")
    from backend.common.settings import reload_settings

    reload_settings()

    captured: dict = {}

    class _Resp:
        status_code = 202
        headers = {"X-Message-Id": "m"}
        text = ""

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

    import backend.common.email_backends.sendgrid as sg

    class _FakeHttpx:
        Client = _Client

        def __getattr__(self, n):
            import httpx

            return getattr(httpx, n)

    monkeypatch.setattr(sg, "httpx", _FakeHttpx())
    sg.send_email_via_sendgrid(
        "a@b.com",
        "s",
        "b",
        custom_args={"prospect_id": "pr_7", "template_id": "t1"},
    )
    assert captured["json"]["custom_args"] == {"prospect_id": "pr_7", "template_id": "t1"}
