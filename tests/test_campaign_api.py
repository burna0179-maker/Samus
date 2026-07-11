"""Campaign API surface tests — status/timeline/artifacts/metrics/audit (§11)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.campaigns import audit as audit_mod
from backend.campaigns import orchestrator as orch_mod
from backend.campaigns.app import create_app
from backend.campaigns.audit import CampaignAuditLedger
from backend.campaigns.models import CampaignInstance
from backend.campaigns.orchestrator import CampaignOrchestrator
from backend.campaigns.store import CampaignRunStore


class FakeDispatcher:
    def dispatch(self, *, node, payload, run):
        if node.id == "audit":
            return {"status": "ok", "kpis": {"page_views": 30}}
        return {"status": "ok"}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
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
    (templates_dir / "linear.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    orch = CampaignOrchestrator(
        store=CampaignRunStore(),
        ledger=CampaignAuditLedger(str(tmp_path / "ledger.jsonl")),
        dispatcher=FakeDispatcher(),
    )
    run = orch.create_campaign(
        CampaignInstance(campaign_id="c1", client_id="acme", template_id="linear", inputs={})
    )
    orch.start(run.campaign_id)

    app = create_app()
    app.state.orchestrator = orch
    client = TestClient(app)
    yield client
    audit_mod.reset_campaign_ledger()


def test_status_endpoint(wired):
    resp = wired.get("/campaigns/c1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign_id"] == "c1"
    assert body["state"] == "completed"
    assert body["completed_nodes"] == ["audit", "collect"]
    assert body["kpis_updated"]["page_views"] == 30


def test_status_unknown_campaign_404(wired):
    assert wired.get("/campaigns/nope/status").status_code == 404


def test_timeline_endpoint(wired):
    body = wired.get("/campaigns/c1/timeline").json()
    assert body["campaign_id"] == "c1"
    assert any(ev.get("node_id") for ev in body["timeline"])


def test_artifacts_endpoint(wired):
    body = wired.get("/campaigns/c1/artifacts").json()
    assert "artifacts" in body


def test_metrics_endpoint(wired):
    body = wired.get("/campaigns/c1/metrics").json()
    assert body["campaign_id"] == "c1"
    kpi_keys = {row["kpi"] for row in body["kpis"]}
    assert "page_views" in kpi_keys


def test_audit_endpoint_reports_chain_integrity(wired):
    body = wired.get("/campaigns/c1/audit").json()
    assert body["chain_intact"] is True
    assert body["events"]


def test_prometheus_metrics_exposed(wired):
    text = wired.get("/metrics").text
    assert "samus_campaign_runs_total" in text


def test_ingest_kpi_via_work_endpoint(wired):
    envelope = {
        "task_id": "t1",
        "payload": {"campaign_id": "c1", "events": [{"kpi": "cta_clicks", "value": 7}]},
        "metadata": {"action": "ingest_kpi"},
    }
    resp = wired.post("/work", json=envelope)
    assert resp.status_code == 200
    assert resp.json()["changed"]["cta_clicks"] == 7
