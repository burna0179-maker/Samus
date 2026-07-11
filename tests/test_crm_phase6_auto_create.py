"""Tests for Phase 6 CRM service auto-create functions.

Stubs out create_opportunity / get_opportunity / create_operator_task
to keep tests as unit-level concerns (no I/O, no filesystem).

Phase 6.
"""
from __future__ import annotations

import pytest

from backend.crm.models import (
    CreateOpportunityResult,
    CreateOperatorTaskResult,
    Opportunity,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _fake_opp(opportunity_id: str = "opp_fake001", stage: str = "new") -> Opportunity:
    return Opportunity(
        opportunity_id=opportunity_id,
        prospect_id="pros_test",
        contact_id="cont_test",
        name="Test Corp",
        stage=stage,
        intent_score=80,
        assigned_to="",
    )


def _ok_create_result(opportunity_id: str = "opp_fake001") -> CreateOpportunityResult:
    return CreateOpportunityResult(
        status="created",
        opportunity_id=opportunity_id,
        ts="2026-01-01T00:00:00Z",
    )


def _ok_task_result(task_id: str = "task_fake001") -> CreateOperatorTaskResult:
    return CreateOperatorTaskResult(
        status="created",
        task_id=task_id,
        ts="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# test_auto_create_from_deal_scoring_cold_tier_skips_creation
# ---------------------------------------------------------------------------

def test_auto_create_from_deal_scoring_cold_tier_skips_creation(monkeypatch):
    """Cold tier from deal_scoring must return status='failed' without creating an Opportunity."""
    import backend.crm.service as svc

    calls: list[str] = []

    def fake_score_deal(intel, engagement=None):
        return {"probability": 0.10, "tier": "cold", "priority_score": 10}

    def fake_create_opportunity(req):
        calls.append("create_opportunity")
        return _ok_create_result()

    import backend.prospecting.deal_scoring as ds_mod
    monkeypatch.setattr(ds_mod, "score_deal", fake_score_deal)
    monkeypatch.setattr(svc, "create_opportunity", fake_create_opportunity)

    result = svc.auto_create_opportunity_from_deal_scoring(
        prospect_id="pros_cold",
        contact_id="cont_cold",
        intel={"opportunity_scores": {}, "signals": {}},
    )

    assert result.status == "failed"
    assert result.error == "cold_tier_no_opportunity_created"
    assert result.opportunity_id == ""
    assert "create_opportunity" not in calls


# ---------------------------------------------------------------------------
# test_auto_create_from_deal_scoring_hot_tier_creates_opportunity
# ---------------------------------------------------------------------------

def test_auto_create_from_deal_scoring_hot_tier_creates_opportunity(monkeypatch):
    """Hot tier must call create_opportunity and return status='created'."""
    import backend.crm.service as svc

    def fake_score_deal(intel, engagement=None):
        return {"probability": 0.85, "tier": "hot", "priority_score": 85}

    def fake_create_opportunity(req):
        return _ok_create_result("opp_hot001")

    def fake_get_opportunity(opp_id):
        return _fake_opp(opp_id, stage="new")

    def fake_create_operator_task(req):
        return _ok_task_result()

    import backend.prospecting.deal_scoring as ds_mod
    monkeypatch.setattr(ds_mod, "score_deal", fake_score_deal)
    monkeypatch.setattr(svc, "create_opportunity", fake_create_opportunity)
    monkeypatch.setattr(svc, "get_opportunity", fake_get_opportunity)
    monkeypatch.setattr(svc, "create_operator_task", fake_create_operator_task)

    result = svc.auto_create_opportunity_from_deal_scoring(
        prospect_id="pros_hot",
        contact_id="cont_hot",
        intel={"opportunity_scores": {"website": 80, "seo": 70}, "signals": {}},
    )

    assert result.status == "created"
    assert result.opportunity_id == "opp_hot001"


# ---------------------------------------------------------------------------
# test_auto_create_runs_lifecycle_task_generators
# ---------------------------------------------------------------------------

def test_auto_create_runs_lifecycle_task_generators(monkeypatch):
    """After creating an opportunity, lifecycle tasks must be generated and persisted."""
    import backend.crm.service as svc

    created_tasks: list[str] = []

    def fake_score_deal(intel, engagement=None):
        return {"probability": 0.70, "tier": "warm", "priority_score": 70}

    def fake_create_opportunity(req):
        return _ok_create_result("opp_warm001")

    def fake_get_opportunity(opp_id):
        # intent_score=70 → should trigger both follow_up AND proposal tasks
        return _fake_opp(opp_id, stage="new")

    def fake_create_operator_task(req):
        created_tasks.append(req.kind)
        return _ok_task_result()

    import backend.prospecting.deal_scoring as ds_mod
    monkeypatch.setattr(ds_mod, "score_deal", fake_score_deal)
    monkeypatch.setattr(svc, "create_opportunity", fake_create_opportunity)
    monkeypatch.setattr(svc, "get_opportunity", fake_get_opportunity)
    monkeypatch.setattr(svc, "create_operator_task", fake_create_operator_task)

    svc.auto_create_opportunity_from_deal_scoring(
        prospect_id="pros_warm",
        contact_id="cont_warm",
        intel={"opportunity_scores": {"website": 70}, "signals": {}},
    )

    # priority_score=70 >= 70 → both follow_up and send_proposal expected
    assert "follow_up" in created_tasks
    assert "send_proposal" in created_tasks


# ---------------------------------------------------------------------------
# test_auto_create_lifecycle_task_failure_does_not_fail_opportunity
# ---------------------------------------------------------------------------

def test_auto_create_lifecycle_task_failure_does_not_fail_opportunity(monkeypatch):
    """A lifecycle task creation failure must not propagate; the opportunity result is returned."""
    import backend.crm.service as svc

    def fake_score_deal(intel, engagement=None):
        return {"probability": 0.60, "tier": "warm", "priority_score": 60}

    def fake_create_opportunity(req):
        return _ok_create_result("opp_resilient")

    def fake_get_opportunity(opp_id):
        return _fake_opp(opp_id, stage="new")

    def fake_create_operator_task_raises(req):
        raise RuntimeError("task persistence failure")

    import backend.prospecting.deal_scoring as ds_mod
    monkeypatch.setattr(ds_mod, "score_deal", fake_score_deal)
    monkeypatch.setattr(svc, "create_opportunity", fake_create_opportunity)
    monkeypatch.setattr(svc, "get_opportunity", fake_get_opportunity)
    monkeypatch.setattr(svc, "create_operator_task", fake_create_operator_task_raises)

    result = svc.auto_create_opportunity_from_deal_scoring(
        prospect_id="pros_resilient",
        contact_id="cont_resilient",
        intel={"opportunity_scores": {}, "signals": {}},
    )

    # The opportunity itself must still be reported as created
    assert result.status == "created"
    assert result.opportunity_id == "opp_resilient"
    assert not result.error  # None on samus tip, "" on prior model — both falsy


# ---------------------------------------------------------------------------
# test_auto_create_from_lead_creates_opportunity
# ---------------------------------------------------------------------------

def test_auto_create_from_lead_creates_opportunity(monkeypatch):
    """auto_create_opportunity_from_lead must delegate to create_opportunity."""
    import backend.crm.service as svc

    created: list[str] = []

    def fake_create_opportunity(req):
        created.append(req.prospect_id)
        return _ok_create_result("opp_lead001")

    def fake_get_opportunity(opp_id):
        return _fake_opp(opp_id, stage="new")

    def fake_create_operator_task(req):
        return _ok_task_result()

    monkeypatch.setattr(svc, "create_opportunity", fake_create_opportunity)
    monkeypatch.setattr(svc, "get_opportunity", fake_get_opportunity)
    monkeypatch.setattr(svc, "create_operator_task", fake_create_operator_task)

    result = svc.auto_create_opportunity_from_lead(
        prospect_id="pros_lead",
        contact_id="cont_lead",
        intent_score=55,
        monthly_budget="$2000",
        service_interest=["seo", "automation"],
        assigned_to="rep@example.com",
    )

    assert result.status == "created"
    assert result.opportunity_id == "opp_lead001"
    assert "pros_lead" in created
