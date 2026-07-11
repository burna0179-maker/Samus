"""Operator opportunity-creation — backend.crm.create_opportunity."""
from __future__ import annotations

import json


def _patch_crm(monkeypatch, *, existing=None, create_status="created"):
    """Stub the CRM service layer so tests never touch DynamoDB.

    ``existing`` is a list of (opportunity_id, prospect_id) pairs the
    pre-check scan will see.
    """
    from backend.crm import service as crm_service
    from backend.crm.models import (
        CreateOpportunityResult,
        Opportunity,
        OpportunityList,
    )
    captured: dict = {}

    def _list_opportunities(*, stage=None, limit=50):
        opps = [
            Opportunity(opportunity_id=oid, prospect_id=pid)
            for oid, pid in (existing or [])
        ]
        return OpportunityList(opportunities=opps, count=len(opps))

    def _create_opportunity(req):
        captured["request"] = req
        return CreateOpportunityResult(
            status=create_status,
            opportunity_id="op_new0001" if create_status == "created" else "",
            ts="2026-05-20T00:00:00Z",
            error=None if create_status == "created" else "ddb_put_failed",
        )

    monkeypatch.setattr(crm_service, "list_opportunities", _list_opportunities)
    monkeypatch.setattr(crm_service, "create_opportunity", _create_opportunity)
    return captured


def test_create_opportunity_mints_a_new_opportunity(monkeypatch):
    captured = _patch_crm(monkeypatch)
    from backend.crm.create_opportunity import create_opportunity

    result = create_opportunity(
        prospect_id="pr_kelly", name="Kelly Z - SEO Audit",
        intent_score=85, service_interest=["seo_audit", "workflow_buildout"],
        next_step="sent the $149 buy link", assigned_to="op@example.com",
    )
    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["opportunity_id"] == "op_new0001"

    req = captured["request"]
    assert req.prospect_id == "pr_kelly"
    assert req.name == "Kelly Z - SEO Audit"
    assert req.intent_score == 85
    assert req.service_interest == ["seo_audit", "workflow_buildout"]
    assert req.next_step == "sent the $149 buy link"
    assert req.assigned_to == "op@example.com"


def test_create_opportunity_rejects_empty_prospect_id(monkeypatch):
    _patch_crm(monkeypatch)
    from backend.crm.create_opportunity import create_opportunity
    result = create_opportunity(prospect_id="   ")
    assert result["ok"] is False
    assert result["status"] == "rejected"


def test_create_opportunity_refuses_duplicate_without_force(monkeypatch):
    captured = _patch_crm(monkeypatch, existing=[("op_old1", "pr_kelly")])
    from backend.crm.create_opportunity import create_opportunity
    result = create_opportunity(prospect_id="pr_kelly", name="dup")
    assert result["ok"] is False
    assert result["status"] == "exists"
    assert result["existing_opportunity_ids"] == ["op_old1"]
    assert "request" not in captured          # no create attempted


def test_create_opportunity_force_overrides_existing(monkeypatch):
    captured = _patch_crm(monkeypatch, existing=[("op_old1", "pr_kelly")])
    from backend.crm.create_opportunity import create_opportunity
    result = create_opportunity(prospect_id="pr_kelly", name="second deal",
                                force=True)
    assert result["ok"] is True
    assert result["opportunity_id"] == "op_new0001"
    assert "request" in captured              # create DID run


def test_create_opportunity_other_prospects_opportunity_does_not_block(monkeypatch):
    """An opportunity on a DIFFERENT prospect must not block this create."""
    _patch_crm(monkeypatch, existing=[("op_other", "pr_someone_else")])
    from backend.crm.create_opportunity import create_opportunity
    result = create_opportunity(prospect_id="pr_kelly")
    assert result["ok"] is True


def test_create_opportunity_degraded_create_is_soft(monkeypatch):
    _patch_crm(monkeypatch, create_status="failed")
    from backend.crm.create_opportunity import create_opportunity
    result = create_opportunity(prospect_id="pr_kelly")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["opportunity_id"] == ""


def test_main_cli_creates_opportunity(monkeypatch, capsys):
    _patch_crm(monkeypatch)
    from backend.crm.create_opportunity import main
    code = main(["--prospect-id", "pr_cli", "--name", "CLI Co",
                 "--intent-score", "85", "--service-interest", "seo_audit"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["opportunity_id"] == "op_new0001"


def test_main_cli_exit_1_on_existing(monkeypatch, capsys):
    _patch_crm(monkeypatch, existing=[("op_old1", "pr_cli")])
    from backend.crm.create_opportunity import main
    code = main(["--prospect-id", "pr_cli"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "exists"
