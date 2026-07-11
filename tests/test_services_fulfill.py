"""Tests for the service-tier fulfillment orchestrator + SLA timer.

All downstream services (CustomerStore, send_email) injected as fakes so tests
run without Neo4j, SendGrid, or the artifact filesystem.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Fakes — match the surface used by backend.memory.customers.CustomerStore
# ---------------------------------------------------------------------------


class _FakeCustomer:
    def __init__(self, id_: str, email: str, name: str = "", current_state: str = "prospect"):
        self.id = id_
        self.email = email
        self.name = name
        self.company = ""
        self.source = "fulfill_service"
        self.created_at = time.time()
        self.current_state = current_state
        self.current_state_since = time.time()
        self.metadata: dict = {}


class _FakeEvent:
    def __init__(self, from_state, to_state):
        self.event_id = "evt_test"
        self.customer_id = "cust_x"
        self.from_state = from_state
        self.to_state = to_state
        self.date = time.time()
        self.reason = ""
        self.metadata: dict = {}


class _FakeCustomerStore:
    def __init__(self):
        self.customers: dict[str, _FakeCustomer] = {}  # by email
        self.by_id: dict[str, _FakeCustomer] = {}
        self.calls: list[tuple] = []

    def get_by_email(self, email):
        self.calls.append(("get_by_email", email))
        return self.customers.get(email.lower())

    def get_customer(self, customer_id):
        self.calls.append(("get_customer", customer_id))
        return self.by_id.get(customer_id)

    def create_customer(self, *, email, name="", company="", source="manual", metadata=None):
        self.calls.append(("create_customer", email))
        cid = f"cust_{email.replace('@', '_at_').replace('.', '_')}"
        cust = _FakeCustomer(id_=cid, email=email.lower(), name=name)
        self.customers[email.lower()] = cust
        self.by_id[cid] = cust
        return cust

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        self.calls.append(("advance_state", customer_id, to_state, reason))
        cust = self.by_id.get(customer_id)
        if cust is None:
            raise ValueError(f"unknown customer: {customer_id}")
        prev = cust.current_state
        cust.current_state = to_state
        return _FakeEvent(from_state=prev, to_state=to_state)

    def list_customers(self, *, state=None, limit=100):
        out = list(self.by_id.values())
        if state is not None:
            out = [c for c in out if c.current_state == state]
        return out[:limit]


def _fake_send_email_fn(captured: list):
    def _fn(*, to, subject, body, html_body=None):
        captured.append({"to": to, "subject": subject, "body": body, "html_body": html_body})
        return {"message_id": "msg_test_123", "channel": "sendgrid", "to": to, "ts": "now"}

    return _fn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_store():
    return _FakeCustomerStore()


@pytest.fixture
def tmp_artifact_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(root))
    return root


@pytest.fixture
def tmp_alert_ledger(tmp_path, monkeypatch):
    p = tmp_path / "sla_alerts.jsonl"
    monkeypatch.setenv("SAMUS_SLA_ALERT_PATH", str(p))
    return p


# ---------------------------------------------------------------------------
# Tests — Workflow Rescue chain (the primary contract)
# ---------------------------------------------------------------------------


def test_workflow_rescue_chain_succeeds(fake_store, tmp_artifact_root):
    """End-to-end: new customer + small bottleneck → all steps OK, SLA armed at 48h."""
    from backend.services.fulfill_service import fulfill_service

    sent: list = []
    intake = {
        "bottleneck": "Squarespace form submissions go to inbox; need them in HubSpot + Slack DM",
        "needs": ["48-Hour Workflow Rescue"],
        "website": "https://acme.example.com",
    }
    result = fulfill_service(
        sku_id="service_workflow_rescue",
        email="buyer@acme.example.com",
        intake_payload=intake,
        name="Buyer",
        customer_store=fake_store,
        send_email_fn=_fake_send_email_fn(sent),
    )

    assert result.ok is True, result.steps
    assert result.sku_id == "service_workflow_rescue"
    assert result.customer_id is not None
    assert result.scope_path is not None
    assert os.path.exists(result.scope_path), "scope.md must be written to disk"
    # SLA deadline ~48h out
    assert result.sla_deadline is not None
    deadline = datetime.strptime(result.sla_deadline, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)
    delta = deadline - now
    assert timedelta(hours=47, minutes=30) < delta < timedelta(hours=48, minutes=30), (
        f"SLA deadline should be ~48h out, got {delta}"
    )
    # Email captured
    assert len(sent) == 1
    assert "Scope confirmation" in sent[0]["subject"]
    assert "48-Hour Workflow Rescue" in sent[0]["subject"]
    # Step trace
    names = [s.name for s in result.steps]
    assert "lookup_sku" in names
    assert "arm_sla" in names
    assert "generate_scope" in names
    assert "validate_scope_gates" in names
    assert "write_scope_artifact" in names
    assert "send_scope_email" in names
    assert "advance_to_scope_confirmed" in names
    # Scope.md content
    scope_text = open(result.scope_path, "r", encoding="utf-8").read()
    assert "Scope Confirmation" in scope_text
    assert "48-Hour Workflow Rescue" in scope_text
    assert "buyer@acme.example.com" in scope_text
    # Operator playbook appended (delivery_template.md content)
    assert "Operator delivery playbook" in scope_text


def test_workflow_rescue_arms_sla_in_customer_metadata(fake_store, tmp_artifact_root):
    """SLA arming persists deadline on the customer's metadata bucket."""
    from backend.services.fulfill_service import fulfill_service
    from backend.services.sla_timer import SLA_METADATA_KEY, get_sla

    sent: list = []
    fulfill_service(
        sku_id="service_workflow_rescue",
        email="rescue@example.com",
        intake_payload={"bottleneck": "lead routing", "needs": []},
        customer_store=fake_store,
        send_email_fn=_fake_send_email_fn(sent),
    )

    cust = fake_store.get_by_email("rescue@example.com")
    assert cust is not None
    sla_bucket = cust.metadata.get(SLA_METADATA_KEY)
    assert sla_bucket is not None, "sla metadata bucket must exist"
    assert "service_workflow_rescue" in sla_bucket
    rec = sla_bucket["service_workflow_rescue"]
    assert rec["sla_hours"] == 48
    assert rec["sla_deadline"] is not None
    assert rec["started_at"] is not None
    assert rec["delivered_at"] is None
    assert rec["alert_fired_at"] is None
    # get_sla reads it back
    fetched = get_sla(fake_store, cust.id, "service_workflow_rescue")
    assert fetched is not None
    assert fetched["sla_hours"] == 48


def test_workflow_rescue_scope_gate_flag_set_when_exceeded(fake_store, tmp_artifact_root):
    """Intake that parses to >5 steps must flag out_of_scope_reason but still complete the chain."""
    from backend.services.fulfill_service import fulfill_service

    # Stuff the intake with phrases that trigger many trigger/action/tool patterns
    intake = {
        "bottleneck": (
            "When a new lead comes in via Squarespace form submission, "
            "send to HubSpot, create a CRM record, post to Slack, "
            "send email confirmation, schedule a followup, create an invoice in Stripe, "
            "append to Notion sheet, send SMS via Twilio, and notify the team on Discord."
        ),
        "needs": ["48-Hour Workflow Rescue"],
    }
    sent: list = []
    result = fulfill_service(
        sku_id="service_workflow_rescue",
        email="big@example.com",
        intake_payload=intake,
        customer_store=fake_store,
        send_email_fn=_fake_send_email_fn(sent),
    )

    assert result.ok is True  # chain completes; flag is informational
    assert result.out_of_scope_reason is not None, (
        "expected scope gate to flag; result was "
        f"{result.out_of_scope_reason!r}, steps={result.steps}"
    )
    # The scope.md should mention the flag
    scope_text = open(result.scope_path, "r", encoding="utf-8").read()
    assert "Scope-gate flag" in scope_text


def test_unknown_sku_rejected(fake_store):
    """fulfill_service must reject an unknown sku_id without touching the customer store."""
    from backend.services.fulfill_service import fulfill_service

    sent: list = []
    result = fulfill_service(
        sku_id="service_nonexistent",
        email="x@example.com",
        intake_payload={"bottleneck": "x"},
        customer_store=fake_store,
        send_email_fn=_fake_send_email_fn(sent),
    )
    assert result.ok is False
    assert result.steps[0].name == "lookup_sku"
    assert result.steps[0].status == "failed"
    # No customer was looked up after the SKU check failed
    assert not any(c[0] == "get_by_email" for c in fake_store.calls)


# ---------------------------------------------------------------------------
# Tests — other SKUs wire through
# ---------------------------------------------------------------------------


def test_workflow_buildout_chain_runs(fake_store, tmp_artifact_root):
    from backend.services.fulfill_service import fulfill_service

    sent: list = []
    result = fulfill_service(
        sku_id="service_workflow_buildout",
        email="biz@example.com",
        intake_payload={
            "bottleneck": "everything is manual",
            "needs": ["Workflow System Buildout"],
        },
        customer_store=fake_store,
        send_email_fn=_fake_send_email_fn(sent),
    )
    assert result.ok is True
    # Buildout has 14-day SLA = 336h
    deadline = datetime.strptime(result.sla_deadline, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    delta = deadline - datetime.now(timezone.utc)
    assert timedelta(hours=335) < delta < timedelta(hours=337)
    # Scope gates DO NOT enforce for Buildout
    names_and_status = [(s.name, s.status) for s in result.steps]
    assert ("validate_scope_gates", "skipped") in names_and_status


def test_seo_implementation_chain_runs(fake_store, tmp_artifact_root):
    from backend.services.fulfill_service import fulfill_service

    sent: list = []
    result = fulfill_service(
        sku_id="service_seo_implementation",
        email="seo@example.com",
        intake_payload={"bottleneck": "apply audit fixes", "needs": ["SEO Audit & Fix"]},
        customer_store=fake_store,
        send_email_fn=_fake_send_email_fn(sent),
    )
    assert result.ok is True
    deadline = datetime.strptime(result.sla_deadline, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    delta = deadline - datetime.now(timezone.utc)
    assert timedelta(hours=167) < delta < timedelta(hours=169)  # 7d = 168h


# ---------------------------------------------------------------------------
# Tests — SLA timer overdue sweep
# ---------------------------------------------------------------------------


def test_sla_check_overdue_returns_true_after_deadline(fake_store, tmp_artifact_root):
    """check_overdue must return True when deadline passed and customer not delivered."""
    from backend.services import sla_timer

    # Seed a customer
    cust = fake_store.create_customer(email="late@example.com")
    # Move to in_delivery (overdue only fires for active deliveries)
    fake_store.advance_state(customer_id=cust.id, to_state="contacted")
    fake_store.advance_state(customer_id=cust.id, to_state="paid")
    fake_store.advance_state(customer_id=cust.id, to_state="in_delivery")

    # Arm with deadline in the past
    past = datetime.now(timezone.utc) - timedelta(hours=49)
    sla_timer.arm_sla(
        customer_store=fake_store,
        customer_id=cust.id,
        sku_id="service_workflow_rescue",
        sla_hours=48,
        started_at=past,
    )
    assert sla_timer.check_overdue(fake_store, cust.id, "service_workflow_rescue") is True

    # Now mark delivered → check_overdue flips False
    fake_store.advance_state(customer_id=cust.id, to_state="delivered")
    assert sla_timer.check_overdue(fake_store, cust.id, "service_workflow_rescue") is False


def test_sla_sweep_overdue_fires_alert_and_dedups(fake_store, tmp_artifact_root, tmp_alert_ledger):
    """sweep_overdue must emit OPERATOR_ALERT_OVERDUE event and not re-fire on subsequent sweeps."""
    from backend.services import sla_timer

    cust = fake_store.create_customer(email="overdue@example.com")
    fake_store.advance_state(customer_id=cust.id, to_state="contacted")
    fake_store.advance_state(customer_id=cust.id, to_state="paid")
    fake_store.advance_state(customer_id=cust.id, to_state="in_delivery")

    past = datetime.now(timezone.utc) - timedelta(hours=49)
    sla_timer.arm_sla(
        customer_store=fake_store,
        customer_id=cust.id,
        sku_id="service_workflow_rescue",
        sla_hours=48,
        started_at=past,
    )

    alerts = sla_timer.sweep_overdue(fake_store)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["event"] == sla_timer.OPERATOR_ALERT_OVERDUE
    assert a["sku_id"] == "service_workflow_rescue"
    assert a["customer_id"] == cust.id
    # Ledger file written
    assert tmp_alert_ledger.exists()
    ledger_lines = [
        json.loads(line) for line in tmp_alert_ledger.read_text().splitlines() if line.strip()
    ]
    assert len(ledger_lines) == 1
    # Re-sweep should NOT re-fire (alert_fired_at gates it)
    alerts2 = sla_timer.sweep_overdue(fake_store)
    assert len(alerts2) == 0


def test_sla_time_remaining_positive_when_in_window(fake_store):
    """time_remaining > 0 inside the SLA window, < 0 past deadline."""
    from backend.services import sla_timer

    cust = fake_store.create_customer(email="onTime@example.com")
    sla_timer.arm_sla(
        customer_store=fake_store,
        customer_id=cust.id,
        sku_id="service_workflow_rescue",
        sla_hours=48,
    )
    remaining = sla_timer.time_remaining(fake_store, cust.id, "service_workflow_rescue")
    assert remaining is not None
    assert remaining > timedelta(hours=47)
    assert remaining < timedelta(hours=48, minutes=1)

    # Re-arm with past start → negative remaining
    sla_timer.arm_sla(
        customer_store=fake_store,
        customer_id=cust.id,
        sku_id="service_workflow_rescue",
        sla_hours=48,
        started_at=datetime.now(timezone.utc) - timedelta(hours=72),
    )
    remaining = sla_timer.time_remaining(fake_store, cust.id, "service_workflow_rescue")
    assert remaining < timedelta(0)
