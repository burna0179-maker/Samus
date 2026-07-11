"""Unified business-event ledger (HOTL Tranche 1) — taxonomy, emit/read
round-trip, fail-soft behavior, and the gateway /admin/journey view.

Ledger isolation follows the conversion-funnel test pattern: point the
SAMUS_BUSINESS_EVENTS_PATH env var at a tmp_path file per test.
"""
from __future__ import annotations

import time

import pytest

from backend.common import business_events as be


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "business_events.jsonl"
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(path))
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    return path


# --- Taxonomy -----------------------------------------------------------

def test_taxonomy_exact_membership():
    assert be.BUSINESS_EVENT_TYPES == frozenset({
        "lead.created", "lead.enriched", "email.sent", "email.opened",
        "email.clicked", "call.placed", "call.answered", "meeting.booked",
        "proposal.sent", "contract.sent", "contract.signed", "invoice.sent",
        "payment.received", "customer.retained", "customer.churned",
        "decision.made", "experiment.assigned", "law.promoted",
        "client.correspondence",
        "calendar.event_scheduled", "calendar.event_completed",
    })


def test_module_constants_match_taxonomy():
    assert be.LEAD_CREATED == "lead.created"
    assert be.DECISION_MADE == "decision.made"
    assert be.PAYMENT_RECEIVED == "payment.received"
    assert be.EXPERIMENT_ASSIGNED == "experiment.assigned"
    assert be.LAW_PROMOTED == "law.promoted"
    for const in (
        be.LEAD_CREATED, be.LEAD_ENRICHED, be.EMAIL_SENT, be.EMAIL_OPENED,
        be.EMAIL_CLICKED, be.CALL_PLACED, be.CALL_ANSWERED, be.MEETING_BOOKED,
        be.PROPOSAL_SENT, be.CONTRACT_SENT, be.CONTRACT_SIGNED, be.INVOICE_SENT,
        be.PAYMENT_RECEIVED, be.CUSTOMER_RETAINED, be.CUSTOMER_CHURNED,
        be.DECISION_MADE, be.EXPERIMENT_ASSIGNED, be.LAW_PROMOTED,
    ):
        assert const in be.BUSINESS_EVENT_TYPES


# --- Emit ---------------------------------------------------------------

def test_emit_stamps_required_fields_and_persists(ledger_path):
    rec = be.emit_business_event(
        be.EMAIL_SENT,
        workcell="outreach",
        prospect_id="p1",
        campaign_id="camp_1",
        variant_arm_id="arm_a",
        cost_usd=0.01,
        revenue_usd=None,
        metadata={"template_id": "t1"},
    )
    assert rec["event_type"] == "email.sent"
    assert rec["workcell"] == "outreach"
    assert rec["prospect_id"] == "p1"
    assert rec["campaign_id"] == "camp_1"
    assert rec["variant_arm_id"] == "arm_a"
    assert rec["cost_usd"] == 0.01
    assert rec["metadata"] == {"template_id": "t1"}
    assert rec["ts"] and rec["trace_id"] and len(rec["event_id"]) == 32
    assert ledger_path.exists()
    rows = be.read_events(prospect_id="p1")
    assert len(rows) == 1
    assert rows[0]["event_id"] == rec["event_id"]


def test_emit_invalid_type_returns_empty_never_raises(ledger_path):
    rec = be.emit_business_event("bogus.event", workcell="outreach")
    assert rec == {}
    assert be.read_events() == []


def test_emit_ledger_failure_is_swallowed(ledger_path, monkeypatch):
    class _Boom:
        def append(self, record):
            raise OSError("disk gone")

    monkeypatch.setattr(be, "_ledger", lambda: _Boom())
    rec = be.emit_business_event(be.LEAD_CREATED, workcell="intake")
    # Record is still returned so callers can inspect it.
    assert rec["event_type"] == "lead.created"


def test_read_failure_returns_empty(ledger_path, monkeypatch):
    class _Boom:
        def tail(self, limit=50):
            raise OSError("disk gone")

    monkeypatch.setattr(be, "_ledger", lambda: _Boom())
    assert be.read_events() == []


# --- Read filters + ordering ---------------------------------------------

def _seed(ledger_path):
    be.emit_business_event(be.LEAD_CREATED, workcell="intake",
                           metadata={"lead_id": "l1"})
    be.emit_business_event(be.LEAD_ENRICHED, workcell="prospecting",
                           prospect_id="p1")
    be.emit_business_event(be.EMAIL_SENT, workcell="outreach",
                           prospect_id="p1", campaign_id="c1")
    be.emit_business_event(be.CALL_PLACED, workcell="voice", prospect_id="p2")
    be.emit_business_event(be.PAYMENT_RECEIVED, workcell="finance",
                           prospect_id="p1", opportunity_id="op_9",
                           revenue_usd=499.0)


def test_read_filters_by_prospect_and_is_chronological(ledger_path):
    _seed(ledger_path)
    rows = be.read_events(prospect_id="p1")
    assert [r["event_type"] for r in rows] == [
        "lead.enriched", "email.sent", "payment.received",
    ]
    # oldest first
    assert rows == sorted(rows, key=lambda r: r["ts"])


def test_read_filters_by_opportunity_and_event_types(ledger_path):
    _seed(ledger_path)
    rows = be.read_events(opportunity_id="op_9")
    assert len(rows) == 1 and rows[0]["revenue_usd"] == 499.0
    rows = be.read_events(event_types=["email.sent", "call.placed"])
    assert {r["event_type"] for r in rows} == {"email.sent", "call.placed"}


def test_read_filters_by_since(ledger_path):
    be.emit_business_event(be.LEAD_CREATED, workcell="intake")
    # iso_now() has 1s resolution; force the next record onto a later second.
    time.sleep(1.1)
    from backend.common.dates import iso_now
    cutoff = iso_now()
    be.emit_business_event(be.EMAIL_SENT, workcell="outreach", prospect_id="p1")
    rows = be.read_events(since=cutoff)
    assert [r["event_type"] for r in rows] == ["email.sent"]


def test_read_limit_keeps_most_recent(ledger_path):
    for i in range(5):
        be.emit_business_event(be.EMAIL_SENT, workcell="outreach",
                               prospect_id="p1", metadata={"n": i})
    rows = be.read_events(prospect_id="p1", limit=2)
    assert [r["metadata"]["n"] for r in rows] == [3, 4]


# --- ConversionRecord campaign_id -----------------------------------------

def test_conversion_record_accepts_and_persists_campaign_id(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SAMUS_OUTREACH_CONVERSIONS_LOG", str(tmp_path / "conv.jsonl"),
    )
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    from backend.finance import outreach_attribution as oa

    rec = oa.record_conversion(
        ref="out_p42", email="x@y.z", amount_usd=99.0,
        event_id="evt_1", campaign_id="camp_7",
    )
    assert rec is not None and rec.campaign_id == "camp_7"
    loaded = oa.load_conversions()
    assert loaded and loaded[0].campaign_id == "camp_7"
    # Backward compat: a record without campaign_id defaults to None.
    rec2 = oa.record_conversion(
        ref="out_p43", email="a@b.c", amount_usd=1.0, event_id="evt_2",
    )
    assert rec2 is not None and rec2.campaign_id is None


# --- Gateway /admin/journey ------------------------------------------------

@pytest.fixture
def gateway_client(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "business_events.jsonl"),
    )
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    from backend.common.settings import reload_settings
    reload_settings()
    from fastapi.testclient import TestClient
    from backend.gateway.app import create_app
    return TestClient(create_app())


def test_admin_journey_returns_chronological_events(gateway_client):
    be.emit_business_event(be.LEAD_ENRICHED, workcell="prospecting",
                           prospect_id="p_j1")
    be.emit_business_event(be.EMAIL_SENT, workcell="outreach",
                           prospect_id="p_j1", campaign_id="c1")
    be.emit_business_event(be.EMAIL_SENT, workcell="outreach",
                           prospect_id="p_other")

    resp = gateway_client.get("/admin/journey/p_j1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prospect_id"] == "p_j1"
    assert body["count"] == 2
    assert [e["event_type"] for e in body["events"]] == [
        "lead.enriched", "email.sent",
    ]


def test_admin_journey_empty_for_unknown_prospect(gateway_client):
    resp = gateway_client.get("/admin/journey/nobody")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == [] and body["count"] == 0
