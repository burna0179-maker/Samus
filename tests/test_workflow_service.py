"""Tests for backend.workflow.service + the fail-soft fulfill_service wiring."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from backend.services.registry import get_sku
from backend.services.scope_planner import generate_scope


def _settings(**over):
    base = dict(workflow_n8n_llm_enrich=False, workflow_n8n_deliverable_enabled=True,
                workflow_n8n_deploy_enabled=False, workflow_n8n_dry_run=True,
                n8n_base_url="", n8n_api_key="")
    base.update(over)
    return SimpleNamespace(**base)


def test_generate_deliverable_writes_files(tmp_path):
    from backend.workflow.service import generate_workflow_deliverable

    artifact = generate_scope(
        {"email": "t@x.com", "bottleneck": "form submission -> Slack + append to Google Sheet"},
        "service_workflow_rescue",
    )
    report = generate_workflow_deliverable(
        artifact, out_dir=tmp_path, settings=_settings(), sku=get_sku("service_workflow_rescue"))

    assert (tmp_path / "workflow.json").exists()
    assert (tmp_path / "runbook.md").exists()
    assert report["valid"] is True
    assert report["node_count"] >= 3
    assert report["deploy"]["status"] == "disabled"  # deploy off
    # The written JSON is parseable n8n shape.
    data = json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))
    assert data["nodes"] and "connections" in data
    # Runbook documents credentials + import steps.
    rb = (tmp_path / "runbook.md").read_text(encoding="utf-8")
    assert "Credentials to configure" in rb
    assert "Import from File" in rb


# --- fail-soft fulfillment wiring ------------------------------------------

class _FakeCustomer:
    def __init__(self, id_, email):
        self.id = id_
        self.email = email
        self.name = "Buyer"
        self.company = ""
        self.current_state = "prospect"
        self.metadata: dict = {}


class _FakeEvent:
    def __init__(self, from_state, to_state):
        self.from_state = from_state
        self.to_state = to_state


class _FakeStore:
    def __init__(self):
        self.by_id = {}
        self.by_email = {}

    def get_by_email(self, email):
        return self.by_email.get(email.lower())

    def get_customer(self, customer_id):
        return self.by_id.get(customer_id)

    def create_customer(self, *, email, name="", company="", source="manual", metadata=None):
        c = _FakeCustomer(f"cust_{email}", email.lower())
        self.by_email[email.lower()] = c
        self.by_id[c.id] = c
        return c

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        c = self.by_id[customer_id]
        prev = c.current_state
        c.current_state = to_state
        return _FakeEvent(prev, to_state)


def _send_email_fn(*, to, subject, body, html_body=None):
    return {"message_id": "msg_1", "channel": "test", "to": to}


def test_fulfillment_workflow_step_is_fail_soft(monkeypatch, tmp_path):
    """A crash in the workflow deliverable must NOT fail the paid scope flow."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    import backend.workflow.service as wf_service

    def boom(*a, **k):
        raise RuntimeError("compiler exploded")

    monkeypatch.setattr(wf_service, "generate_workflow_deliverable", boom)

    from backend.services.fulfill_service import fulfill_service

    result = fulfill_service(
        sku_id="service_workflow_rescue",
        email="buyer@acme.example.com",
        intake_payload={"bottleneck": "form -> slack", "needs": ["48-Hour Workflow Rescue"]},
        customer_store=_FakeStore(),
        send_email_fn=_send_email_fn,
    )

    assert result.ok is True  # paid flow still completes
    step = next(s for s in result.steps if s.name == "write_workflow_artifact")
    assert step.status == "failed"
    assert "compiler exploded" in step.detail


def test_fulfillment_writes_workflow_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.services.fulfill_service import fulfill_service

    result = fulfill_service(
        sku_id="service_workflow_rescue",
        email="buyer2@acme.example.com",
        intake_payload={"bottleneck": "Calendly booking -> Twilio SMS -> Discord on failure",
                        "needs": ["48-Hour Workflow Rescue"]},
        customer_store=_FakeStore(),
        send_email_fn=_send_email_fn,
    )
    assert result.ok is True
    assert result.workflow_path is not None
    import os
    assert os.path.exists(result.workflow_path)
    step = next(s for s in result.steps if s.name == "write_workflow_artifact")
    assert step.status == "ok"
