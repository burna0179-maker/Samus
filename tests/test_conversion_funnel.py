"""Tests for the conversion-funnel telemetry ledger.

Covers backend/common/conversion_funnel.py — stage recording, the
leak-analysis snapshot aggregation, graceful degradation on a bad stage or a
filesystem error — and the CRM lifecycle hooks + GET /crm/metrics/funnel route.
"""
from __future__ import annotations

import pytest

from backend.common import conversion_funnel as funnel


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    """Point the funnel ledger at a per-test temp file."""
    monkeypatch.setenv(
        "SAMUS_CONVERSION_FUNNEL_PATH", str(tmp_path / "funnel.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# record_stage
# ---------------------------------------------------------------------------

def test_record_stage_accepts_known_stage():
    assert funnel.record_stage("prospect", entity_id="pr_1") is True


def test_record_stage_rejects_unknown_stage():
    # A typo upstream must not poison the ledger — no-op + False.
    assert funnel.record_stage("propsect") is False


def test_record_stage_all_funnel_stages_known():
    for stage in funnel.FUNNEL_STAGES:
        assert funnel.record_stage(stage) is True


def test_record_stage_degrades_on_filesystem_error(monkeypatch):
    class _BoomLedger:
        def append(self, _record):
            raise OSError("disk gone")

    monkeypatch.setattr(funnel, "_ledger", lambda: _BoomLedger())
    # Never raises — returns False.
    assert funnel.record_stage("prospect") is False


# ---------------------------------------------------------------------------
# funnel_snapshot
# ---------------------------------------------------------------------------

def test_snapshot_empty_ledger_is_all_zero():
    snap = funnel.funnel_snapshot()
    assert snap["total_events"] == 0
    assert all(v == 0 for v in snap["stages"].values())
    assert snap["overall_conversion_rate"] == 0.0
    assert snap["error"] is None
    # Every forward transition is present even with no data.
    assert len(snap["transitions"]) == 4


def test_snapshot_counts_per_stage():
    for _ in range(10):
        funnel.record_stage("lead")
    for _ in range(6):
        funnel.record_stage("prospect")
    for _ in range(3):
        funnel.record_stage("opportunity")
    funnel.record_stage("closed_won")

    snap = funnel.funnel_snapshot()
    assert snap["stages"]["lead"] == 10
    assert snap["stages"]["prospect"] == 6
    assert snap["stages"]["opportunity"] == 3
    assert snap["stages"]["closed_won"] == 1
    assert snap["total_events"] == 20


def test_snapshot_transition_conversion_rates():
    for _ in range(10):
        funnel.record_stage("lead")
    for _ in range(5):
        funnel.record_stage("prospect")

    snap = funnel.funnel_snapshot()
    lead_to_prospect = next(
        t for t in snap["transitions"]
        if t["from"] == "lead" and t["to"] == "prospect"
    )
    assert lead_to_prospect["conversion_rate"] == 0.5
    assert lead_to_prospect["leaked"] == 5
    assert lead_to_prospect["from_count"] == 10
    assert lead_to_prospect["to_count"] == 5


def test_snapshot_overall_conversion_rate():
    for _ in range(20):
        funnel.record_stage("lead")
    for _ in range(4):
        funnel.record_stage("closed_won")

    snap = funnel.funnel_snapshot()
    # 4 wins / 20 leads.
    assert snap["overall_conversion_rate"] == 0.2


def test_snapshot_no_divide_by_zero_when_upstream_empty():
    funnel.record_stage("prospect")  # downstream with no upstream lead.
    snap = funnel.funnel_snapshot()
    lead_to_prospect = next(
        t for t in snap["transitions"] if t["from"] == "lead"
    )
    assert lead_to_prospect["conversion_rate"] == 0.0
    assert snap["overall_conversion_rate"] == 0.0


def test_snapshot_degrades_on_read_error(monkeypatch):
    class _BoomLedger:
        def tail(self, limit=0):
            raise RuntimeError("read exploded")

    monkeypatch.setattr(funnel, "_ledger", lambda: _BoomLedger())
    snap = funnel.funnel_snapshot()
    assert snap["error"] is not None
    assert "funnel_read_failed" in snap["error"]
    assert snap["total_events"] == 0


# ---------------------------------------------------------------------------
# CRM lifecycle integration — the hook fires on real lifecycle calls.
# ---------------------------------------------------------------------------

def _crm_setup(monkeypatch):
    """Import the CRM service test shims to drive a real lifecycle call."""
    from tests.test_crm_service import _patch_tables, _audit_to_tmp
    return _patch_tables, _audit_to_tmp


def test_create_opportunity_records_funnel_stage(monkeypatch, tmp_path):
    from tests.test_crm_service import _patch_tables
    from backend.crm import service as svc
    from backend.crm.models import CreateOpportunityRequest

    _patch_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    result = svc.create_opportunity(CreateOpportunityRequest(
        prospect_id="pr_1", industry="plumbing",
    ))
    assert result.status == "created"

    snap = funnel.funnel_snapshot()
    assert snap["stages"]["opportunity"] == 1


def test_advance_opportunity_records_terminal_funnel_stage(monkeypatch, tmp_path):
    from tests.test_crm_service import _patch_tables
    from backend.crm import service as svc
    from backend.crm.models import (
        AdvanceOpportunityRequest, CreateOpportunityRequest,
    )

    _patch_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # The default close-cap ($1000) would block the $1500 closed_won
    # transition below; raise the cap so this funnel-stage test isn't
    # gated by an unrelated guard.
    monkeypatch.setenv("SAMUS_CRM_MAX_CLOSE_AMOUNT_USD", "100000")
    from backend.common.settings import reload_settings
    reload_settings()

    created = svc.create_opportunity(CreateOpportunityRequest(
        prospect_id="pr_1", industry="hvac",
    ))
    # new -> qualified -> proposal -> closed_won (FSM-legal forward path).
    svc.advance_opportunity(AdvanceOpportunityRequest(
        opportunity_id=created.opportunity_id, target_stage="qualified",
    ))
    svc.advance_opportunity(AdvanceOpportunityRequest(
        opportunity_id=created.opportunity_id, target_stage="proposal",
    ))
    svc.advance_opportunity(AdvanceOpportunityRequest(
        opportunity_id=created.opportunity_id, target_stage="closed_won",
        won_amount_usd=1500.0,
    ))

    snap = funnel.funnel_snapshot()
    assert snap["stages"]["opportunity"] == 1
    assert snap["stages"]["proposal"] == 1
    assert snap["stages"]["closed_won"] == 1


# ---------------------------------------------------------------------------
# GET /crm/metrics/funnel route
# ---------------------------------------------------------------------------

def test_crm_funnel_route_returns_snapshot():
    from fastapi.testclient import TestClient
    from backend.crm.app import app

    funnel.record_stage("lead")
    funnel.record_stage("prospect")

    r = TestClient(app).get("/crm/metrics/funnel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stages"]["lead"] == 1
    assert body["stages"]["prospect"] == 1
    assert "transitions" in body
    assert "overall_conversion_rate" in body
