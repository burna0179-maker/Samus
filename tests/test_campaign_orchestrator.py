"""Campaign orchestration lifecycle tests (deliverable §11).

Covers: deterministic node execution, approval-gate pause + resume, audit event
emission, KPI updates, unsafe public-action rejection, and creating a real
Sample School campaign instance from the shipped template.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backend.campaigns import audit as audit_mod
from backend.campaigns import orchestrator as orch_mod
from backend.campaigns.audit import CampaignAuditLedger
from backend.campaigns.models import (
    ApprovalLevel,
    CampaignInstance,
    CampaignNode,
    CampaignRun,
    CampaignState,
    NodeStatus,
)
from backend.campaigns.orchestrator import CampaignOrchestrator
from backend.campaigns.store import CampaignRunStore
from backend.campaigns.templates import load_instance, load_template
from backend.common import approvals

_ROOT = Path(__file__).resolve().parents[1]
_SCHOOL = _ROOT / "backend" / "campaigns" / "templates" / "school_enrollment_campaign.yaml"
# Conquerors' instance was pivoted 2026-07-10 to school_phased_maintenance;
# the accelerated-template create test now loads its OWN synthetic instance
# so it exercises the accelerated template independent of the client file.
_PHASED = _ROOT / "backend" / "campaigns" / "templates" / "school_phased_maintenance.yaml"
_CONQUERORS = _ROOT / "clients" / "sample_school" / "campaign.yaml"


class FakeDispatcher:
    """Deterministic dispatcher — records calls and returns canned results."""

    def __init__(self, results: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._results = results or {}

    def dispatch(self, *, node, payload, run):
        self.calls.append((node.id, node.capability))
        return self._results.get(node.id, {"status": "ok"})


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point every campaign store/ledger/approval sink at a tmp dir."""
    monkeypatch.setenv("SAMUS_ENV", "development")
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_CAMPAIGN_RUNS_PATH", str(tmp_path / "runs.json"))
    monkeypatch.setenv("SAMUS_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("SAMUS_CAMPAIGN_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SAMUS_CAMPAIGN_REPORTS_DIR", str(tmp_path / "reports"))
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    monkeypatch.setenv("SAMUS_CAMPAIGN_TEMPLATES_DIR", str(templates_dir))
    audit_mod.reset_campaign_ledger()
    orch_mod.reset_default_orchestrator()
    yield tmp_path
    audit_mod.reset_campaign_ledger()


def _write_template(templates_dir: Path, data: dict) -> None:
    path = templates_dir / f"{data['template_id']}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _linear_template(**over) -> dict:
    data = {
        "template_id": "linear",
        "vertical": "test",
        "required_inputs": ["client_id"],
        "kpis": [{"key": "page_views"}],
        "nodes": [
            {"id": "audit", "type": "seo_audit", "target_workcell": "seo",
             "capability": "audit_and_report", "approval_required": "none"},
            {"id": "collect", "type": "metrics_collection", "target_workcell": "campaigns",
             "capability": "update_kpis", "approval_required": "none"},
        ],
        "edges": [{"from": "audit", "to": "collect"}],
    }
    data.update(over)
    return data


def _make_orch(dispatcher, tmp_path) -> CampaignOrchestrator:
    return CampaignOrchestrator(
        store=CampaignRunStore(),
        ledger=CampaignAuditLedger(str(tmp_path / "ledger.jsonl")),
        dispatcher=dispatcher,
    )


def _instance(template_id="linear", campaign_id="c1") -> CampaignInstance:
    return CampaignInstance(
        campaign_id=campaign_id, client_id="acme", template_id=template_id, inputs={}
    )


# --- executing a deterministic node ---------------------------------------


def test_executes_deterministic_node(isolated):
    disp = FakeDispatcher({"audit": {"status": "ok", "kpis": {"page_views": 42}}})
    orch = _make_orch(disp, isolated)
    _write_template(isolated / "templates", _linear_template())

    run = orch.create_campaign(_instance())
    assert run.state == CampaignState.READY
    run = orch.start(run.campaign_id)

    assert run.state == CampaignState.COMPLETED
    assert run.completed_nodes == ["audit", "collect"]
    assert run.kpis_updated["page_views"] == 42
    assert disp.calls[0] == ("audit", "audit_and_report")


# --- emitting audit events -------------------------------------------------


def test_emits_audit_events_with_required_schema(isolated):
    orch = _make_orch(FakeDispatcher(), isolated)
    _write_template(isolated / "templates", _linear_template())
    run = orch.create_campaign(_instance())
    orch.start(run.campaign_id)

    events = orch.timeline(run.campaign_id)
    node_events = [e for e in events if e.get("node_id")]
    assert node_events, "expected per-node audit events"
    required = {
        "event_id", "trace_id", "campaign_id", "client_id", "node_id",
        "node_type", "target_workcell", "capability", "input_hash",
        "output_hash", "status", "severity", "approval_state", "timestamp",
        "duration_ms", "error_summary", "artifact_refs", "kpi_refs",
    }
    for ev in node_events:
        assert required.issubset(ev.keys()), required - ev.keys()
    assert orch.ledger.verify() is True  # hash chain intact


# --- pausing at approval-gated nodes + resuming ---------------------------


def test_pauses_at_approval_gate_then_resumes(isolated):
    tpl = _linear_template(
        template_id="gated",
        nodes=[
            {"id": "audit", "type": "seo_audit", "target_workcell": "seo",
             "capability": "audit_and_report", "approval_required": "none"},
            {"id": "plan", "type": "funnel_plan", "target_workcell": "proposal",
             "capability": "generate_proposal", "approval_required": "operator"},
        ],
        edges=[{"from": "audit", "to": "plan"}],
    )
    _write_template(isolated / "templates", tpl)
    orch = _make_orch(FakeDispatcher(), isolated)
    run = orch.create_campaign(_instance(template_id="gated"))

    run = orch.start(run.campaign_id)
    assert run.state == CampaignState.NEEDS_APPROVAL
    assert "plan" in run.needs_approval_nodes
    assert "plan" not in run.completed_nodes
    assert len(run.approvals_required) == 1

    # a pending approval exists in the HOTL queue
    approval_id = run.approvals_required[0]
    assert approvals.get_approval(approval_id)["status"] == approvals.STATUS_PENDING

    # operator approves -> node runs, campaign completes
    run = orch.approve_node(run.campaign_id, node_id="plan", decision="approved")
    assert run.state == CampaignState.COMPLETED
    assert "plan" in run.completed_nodes


def test_rejecting_approval_blocks_the_node(isolated):
    tpl = _linear_template(
        template_id="gated2",
        nodes=[
            {"id": "plan", "type": "funnel_plan", "target_workcell": "proposal",
             "capability": "generate_proposal", "approval_required": "operator"},
        ],
        edges=[],
    )
    _write_template(isolated / "templates", tpl)
    orch = _make_orch(FakeDispatcher(), isolated)
    run = orch.create_campaign(_instance(template_id="gated2"))
    run = orch.start(run.campaign_id)
    assert run.state == CampaignState.NEEDS_APPROVAL

    run = orch.approve_node(run.campaign_id, node_id="plan", decision="rejected")
    assert run.state == CampaignState.BLOCKED
    assert "plan" in run.blocked_nodes


# --- rejecting unsafe public-action nodes without approval ----------------


def test_rejects_unsafe_public_action_without_approval(isolated):
    tpl = _linear_template(
        template_id="unsafe",
        nodes=[
            {"id": "post", "type": "public_action", "target_workcell": "outreach",
             "capability": "send_message", "approval_required": "none"},
        ],
        edges=[],
    )
    _write_template(isolated / "templates", tpl)
    disp = FakeDispatcher()
    orch = _make_orch(disp, isolated)
    run = orch.create_campaign(_instance(template_id="unsafe"))
    run = orch.start(run.campaign_id)

    assert run.state == CampaignState.BLOCKED
    assert "post" in run.blocked_nodes
    assert run.step_results["post"].status == NodeStatus.BLOCKED
    assert run.step_results["post"].error_summary == "high_risk_node_requires_approval"
    # the unsafe node was NEVER dispatched
    assert disp.calls == []


# --- updating KPI values ---------------------------------------------------


def test_updates_kpi_values_manual_and_webhook(isolated):
    from backend.campaigns import kpi

    orch = _make_orch(FakeDispatcher(), isolated)
    _write_template(isolated / "templates", _linear_template())
    run = orch.create_campaign(_instance())

    changed = kpi.apply_kpi_events(
        run,
        [
            {"kpi": "page_views", "mode": "set", "value": 100},
            {"kpi": "page_views", "mode": "increment", "value": 25},
            {"kpi": "cta_clicks", "mode": "increment", "value": 5},
        ],
    )
    assert run.kpis_updated["page_views"] == 125
    assert run.kpis_updated["cta_clicks"] == 5
    assert changed["page_views"] == 125

    with pytest.raises(ValueError, match="unknown KPI"):
        kpi.apply_kpi_events(run, [{"kpi": "not_a_kpi", "value": 1}])


# --- creating a Sample School campaign instance -------------


def test_creates_conquerors_campaign_instance(isolated):
    # Conquerors pivoted to phased_maintenance 2026-07-10 — this test now
    # validates the phased binding (which is the live client posture).
    template = load_template(_PHASED)
    instance = load_instance(_CONQUERORS)
    orch = _make_orch(FakeDispatcher(), isolated)

    run = orch.create_campaign(instance, template=template)
    assert isinstance(run, CampaignRun)
    assert run.client_id == "sample_school"
    assert run.template_id == "school_phased_maintenance"
    assert run.state == CampaignState.READY
    assert run.vertical == "education"
    # instance inputs are bound into the run context (no hardcoded client logic)
    assert run.context["inputs"]["school_name"] == "The Sample School"
    # Phased posture -> modest ongoing targets
    assert run.context["kpi_targets"]["page_views"] == 500
    assert run.context["kpi_targets"]["tour_requests"] == 5


def test_generates_weekly_report_from_run_state(isolated):
    import json

    from backend.campaigns import kpi
    from backend.campaigns.audit import CampaignAuditLedger
    from backend.campaigns.dispatch import LocalCapabilityDispatcher
    from backend.campaigns.report import generate_weekly_report

    ledger = CampaignAuditLedger(str(isolated / "ledger.jsonl"))
    run = CampaignRun(
        campaign_id="rc", client_id="acme", template_id="school_enrollment_campaign",
        vertical="education", state=CampaignState.RUNNING,
    )
    run.completed_nodes = ["intake_assets", "audit_website"]
    kpi.apply_kpi_events(run, [{"kpi": "page_views", "mode": "set", "value": 900}])

    # local dispatch path: metrics_collection + reporting run in-process
    local = LocalCapabilityDispatcher(ledger)
    snap = local.dispatch(
        node=CampaignNode(id="collect", type="metrics_collection",
                          target_workcell="campaigns", capability="update_kpis"),
        payload={}, run=run,
    )
    assert snap["summary"] == "kpi_snapshot"

    artifact = generate_weekly_report(run, ledger)
    assert artifact.type == "weekly_report"
    body = json.loads(Path(artifact.ref).read_text(encoding="utf-8"))
    for section in (
        "executive_summary", "kpi_table", "completed_actions", "blocked_actions",
        "next_actions", "top_performing_channels", "weak_points",
        "recommendations", "approval_requests", "artifact_links",
    ):
        assert section in body
    assert body["completed_actions"] == ["intake_assets", "audit_website"]
    assert any(row["kpi"] == "page_views" for row in body["kpi_table"])


def test_create_rejects_missing_required_inputs(isolated):
    template = load_template(_SCHOOL)
    orch = _make_orch(FakeDispatcher(), isolated)
    bad = CampaignInstance(
        campaign_id="x", client_id="c", template_id="school_enrollment_campaign",
        inputs={"school_name": "S"},  # missing website_url, enrollment_deadline, ...
    )
    with pytest.raises(orch_mod.CampaignError, match="missing required inputs"):
        orch.create_campaign(bad, template=template)
