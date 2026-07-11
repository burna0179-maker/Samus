"""Site telemetry ingest — wire-not-arm gating + JSONL persistence."""
from __future__ import annotations

import json
from types import SimpleNamespace

import backend.intake.telemetry as tel
from backend.intake.models import TelemetryEventRequest


def _fake_get_settings(*, enabled: bool):
    def _gs():
        return SimpleNamespace(intake_telemetry_ingest_enabled=enabled)
    _gs.cache_clear = lambda: None
    return _gs


def _req(**kw):
    base = dict(
        event="page_view",
        path="/",
        referrer="",
        session_id="sess_xyz",
        ts="2026-07-03T12:00:00Z",
        sku_id="",
    )
    base.update(kw)
    return TelemetryEventRequest(**base)


def test_dropped_when_flag_off(tmp_path, monkeypatch):
    """OFF -> 200 with dropped_disabled, no ledger write. Beacon must never
    look broken to the customer while the capability is dormant."""
    monkeypatch.setattr(tel, "get_settings", _fake_get_settings(enabled=False))
    ledger = tmp_path / "site_telemetry.jsonl"
    monkeypatch.setenv("SAMUS_INTAKE_TELEMETRY_PATH", str(ledger))
    out = tel.record_event(_req(), source_ip="1.2.3.4", user_agent="ua")
    assert out.status == "dropped_disabled"
    assert not ledger.exists(), "OFF must NOT persist"


def test_persists_when_armed(tmp_path, monkeypatch):
    """ON -> row lands in the JSONL ledger with server-authoritative received_at."""
    monkeypatch.setattr(tel, "get_settings", _fake_get_settings(enabled=True))
    ledger = tmp_path / "site_telemetry.jsonl"
    monkeypatch.setenv("SAMUS_INTAKE_TELEMETRY_PATH", str(ledger))
    out = tel.record_event(
        _req(event="buy_click", path="/pricing", sku_id="seo_audit"),
        source_ip="1.2.3.4", user_agent="Mozilla/5.0",
    )
    assert out.status == "accepted"
    assert ledger.exists()
    rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "buy_click"
    assert row["path"] == "/pricing"
    assert row["sku_id"] == "seo_audit"
    assert row["source_ip"] == "1.2.3.4"
    assert row["received_at"]  # server stamp present


def test_extra_field_rejected():
    """extra='forbid' — a compromised site can't sneak arbitrary keys into
    the ledger row."""
    import pytest
    with pytest.raises(Exception):
        TelemetryEventRequest(event="page_view", sneaky_field="hack")


def test_bounded_event_types():
    """Only whitelisted events accepted. A future ad-hoc event would need to
    be added to the SiteEventType Literal deliberately."""
    import pytest
    with pytest.raises(Exception):
        TelemetryEventRequest(event="arbitrary_key")
