"""Smoke tests for the digital-product fulfillment chain.

Mirrors test_fulfill_orchestrator.py — fakes for CustomerStore and
send_email so the test runs without Neo4j or SendGrid.

Covers:
  - Happy-path fulfillment for one playbook (file copy + inline email)
  - Happy-path fulfillment for one pack (zip + attachment email)
  - Registry lookup failure surfaces clearly
  - Unknown SKU short-circuits before touching the customer store
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Fakes — same pattern as test_fulfill_orchestrator.py
# ---------------------------------------------------------------------------


class _FakeCustomer:
    def __init__(self, id_: str, email: str, name: str = "", current_state: str = "prospect"):
        self.id = id_
        self.email = email
        self.name = name
        self.company = ""
        self.source = "digital_fulfill_test"
        self.created_at = time.time()
        self.current_state = current_state
        self.current_state_since = time.time()
        self.metadata: dict = {}


class _FakeEvent:
    def __init__(self, from_state: str | None, to_state: str):
        self.event_id = "evt_test"
        self.customer_id = "cust_x"
        self.from_state = from_state
        self.to_state = to_state
        self.date = time.time()
        self.reason = ""
        self.metadata: dict = {}


class _FakeCustomerStore:
    def __init__(self):
        self.customers: dict[str, _FakeCustomer] = {}
        self.calls: list[tuple] = []

    def get_by_email(self, email: str):
        self.calls.append(("get_by_email", email))
        return self.customers.get(email.lower())

    def create_customer(
        self,
        *,
        email: str,
        name: str = "",
        company: str = "",
        source: str = "manual",
        metadata: dict | None = None,
    ):
        self.calls.append(("create_customer", email, source))
        cust = _FakeCustomer(
            id_=f"cust_{email.replace('@', '_at_').replace('.', '_')}",
            email=email,
            name=name,
            current_state="prospect",
        )
        self.customers[email.lower()] = cust
        return cust

    def advance_state(
        self, *, customer_id: str, to_state: str, reason: str = "", metadata: dict | None = None
    ):
        self.calls.append(("advance_state", customer_id, to_state, reason))
        for cust in self.customers.values():
            if cust.id == customer_id:
                from_state = cust.current_state
                cust.current_state = to_state
                return _FakeEvent(from_state=from_state, to_state=to_state)
        raise ValueError(f"unknown customer_id: {customer_id}")


def _fake_send_email_capture(captured: list, message_id: str = "sg_test_123"):
    def _fn(*, to, subject, body, attachments=None):
        captured.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "attachments": attachments,
            }
        )
        return {
            "message_id": message_id,
            "channel": "sendgrid",
            "to": to,
            "ts": "2026-05-16T00:00:00Z",
        }

    return _fn


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_playbook_fulfillment_writes_md_and_inlines_body(tmp_path: Path):
    from backend.products import fulfill_digital_product

    store = _FakeCustomerStore()
    captured: list = []

    result = fulfill_digital_product(
        sku_id="playbook_lead_qual",
        email="alice@example.com",
        name="Alice",
        customer_store=store,
        send_email_fn=_fake_send_email_capture(captured),
        artifact_root=tmp_path,
    )

    assert result.ok is True
    assert result.sku_id == "playbook_lead_qual"
    assert result.customer_id.startswith("cust_alice")
    assert result.final_state == "delivered"
    assert result.email_message_id == "sg_test_123"

    step_names = [s.name for s in result.steps]
    assert step_names == [
        "lookup_sku",
        "find_or_create_customer",
        "advance_to_in_delivery",
        "produce_artifact",
        "send_email",
        "advance_to_delivered",
    ]
    assert all(s.status == "ok" for s in result.steps)

    artifact = Path(result.artifact_path)
    assert artifact.exists()
    assert artifact.name == "playbook_lead_qual.md"
    assert artifact.parent.name.startswith("cust_alice")
    content = artifact.read_text(encoding="utf-8")
    assert "Lead Qualification Workflow Playbook" in content

    assert len(captured) == 1
    sent = captured[0]
    assert sent["to"] == "alice@example.com"
    assert sent["subject"] == "Your Lead Qualification Workflow Playbook"
    assert "Hi Alice," in sent["body"]
    assert "Lead Qualification Workflow Playbook" in sent["body"]
    assert sent["attachments"] is None

    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert [c[2] for c in advances] == ["in_delivery", "delivered"]


def test_pack_fulfillment_zips_bundle_and_attaches(tmp_path: Path):
    from backend.products import fulfill_digital_product

    store = _FakeCustomerStore()
    captured: list = []

    result = fulfill_digital_product(
        sku_id="pack_creator_quickstart",
        email="bob@example.com",
        name="Bob",
        customer_store=store,
        send_email_fn=_fake_send_email_capture(captured, message_id="sg_pack"),
        artifact_root=tmp_path,
    )

    assert result.ok is True
    assert result.sku_id == "pack_creator_quickstart"
    assert result.final_state == "delivered"
    assert result.email_message_id == "sg_pack"

    assert all(s.status == "ok" for s in result.steps)

    zip_path = Path(result.artifact_path)
    assert zip_path.exists()
    assert zip_path.suffix == ".zip"
    pack_dir = zip_path.parent / "pack_creator_quickstart"
    assert pack_dir.is_dir()
    readme = pack_dir / "README.md"
    assert readme.exists()
    assert "Creator QuickStart Pack" in readme.read_text(encoding="utf-8")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any(n.endswith("README.md") for n in names)
    assert any("01_content_calendar_template.md" in n for n in names)
    assert all(n.startswith("pack_creator_quickstart/") for n in names)

    assert len(captured) == 1
    sent = captured[0]
    assert sent["to"] == "bob@example.com"
    assert sent["subject"] == "Your Creator QuickStart Pack"
    assert "Hi Bob," in sent["body"]
    assert sent["attachments"] is not None
    assert len(sent["attachments"]) == 1
    att = sent["attachments"][0]
    assert att["filename"] == "pack_creator_quickstart.zip"
    assert att["mime_type"] == "application/zip"
    assert isinstance(att["content"], bytes)
    assert len(att["content"]) > 0


def test_addon_fulfillment_renders_brief_and_inlines(tmp_path: Path):
    from backend.products import fulfill_digital_product

    store = _FakeCustomerStore()
    captured: list = []

    result = fulfill_digital_product(
        sku_id="addon_stripe_hardening",
        email="carol@example.com",
        name="Carol",
        customer_store=store,
        send_email_fn=_fake_send_email_capture(captured),
        artifact_root=tmp_path,
    )

    assert result.ok is True
    artifact = Path(result.artifact_path)
    assert artifact.exists()
    assert artifact.suffix == ".md"
    body_text = artifact.read_text(encoding="utf-8")
    assert "Stripe Webhook Hardening" in body_text
    assert "HMAC" in body_text

    sent = captured[0]
    assert sent["attachments"] is None
    assert "Stripe Webhook Hardening" in sent["body"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_unknown_sku_fails_at_lookup_without_touching_store(tmp_path: Path):
    from backend.products import fulfill_digital_product

    store = _FakeCustomerStore()
    captured: list = []

    result = fulfill_digital_product(
        sku_id="playbook_does_not_exist",
        email="dave@example.com",
        customer_store=store,
        send_email_fn=_fake_send_email_capture(captured),
        artifact_root=tmp_path,
    )

    assert result.ok is False
    assert len(result.steps) == 1
    assert result.steps[0].name == "lookup_sku"
    assert result.steps[0].status == "failed"
    assert "playbook_does_not_exist" in result.steps[0].detail
    assert store.calls == []
    assert captured == []


def test_send_email_failure_leaves_state_in_delivery(tmp_path: Path):
    from backend.products import fulfill_digital_product

    store = _FakeCustomerStore()

    def _failing_send(*, to, subject, body, attachments=None):
        raise RuntimeError("sendgrid 503 throttled")

    result = fulfill_digital_product(
        sku_id="playbook_sales_followup",
        email="ed@example.com",
        customer_store=store,
        send_email_fn=_failing_send,
        artifact_root=tmp_path,
    )

    assert result.ok is False
    statuses = {s.name: s.status for s in result.steps}
    assert statuses["produce_artifact"] == "ok"
    assert statuses["send_email"] == "failed"
    assert "advance_to_delivered" not in statuses
    assert result.final_state == "in_delivery"
    assert Path(result.artifact_path).exists()


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_registry_exposes_all_seven_sku_ids():
    from backend.products import PRODUCTS, ADDONS

    product_ids = {p.sku_id for p in PRODUCTS}
    assert product_ids == {
        "playbook_lead_qual",
        "playbook_client_onboarding",
        "playbook_sales_followup",
        "pack_creator_quickstart",
        "pack_content_funnel",
        "pack_authority_accelerator",
    }
    addon_ids = {a.sku_id for a in ADDONS}
    assert addon_ids == {
        "addon_stripe_hardening",
        "addon_email_deliverability",
        "addon_automation_health_check",
        "addon_crm_hygiene_sweep",
        "addon_dashboard_setup",
        "addon_404_audit",
        "addon_dns_health",
    }


def test_registry_leaves_stripe_ids_unset_for_stream4():
    from backend.products import PRODUCTS, ADDONS

    for p in PRODUCTS:
        assert p.stripe_product_id is None, (
            f"{p.sku_id}: stripe_product_id should be left None for Stream 4"
        )
        assert p.stripe_price_id is None
    for a in ADDONS:
        assert a.stripe_product_id is None
        assert a.stripe_price_id is None
