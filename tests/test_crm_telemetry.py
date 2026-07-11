"""Tests for the CRM telemetry extensions (additive, 2026-05-20).

Covers three observability features in backend/crm/service.py + their
GET /crm/metrics/* routes:

  1. latency_to_resolution_sec — deal resolution latency on a terminal advance
  2. estimated_close_probability — empirical per-industry / per-stage rates
  3. token_cost_by_industry — per-vertical discovery-LLM cost roll-up

The CRM table seam is mocked with the shims from test_crm_service.
"""

from __future__ import annotations

import json

import pytest

from tests.test_crm_service import _patch_tables


@pytest.fixture(autouse=True)
def _funnel_tmp(tmp_path, monkeypatch):
    """Keep the conversion-funnel ledger out of /opt/samus during these tests."""
    monkeypatch.setenv(
        "SAMUS_CONVERSION_FUNNEL_PATH",
        str(tmp_path / "funnel.jsonl"),
    )


# ===========================================================================
# Feature 1 — latency_to_resolution_sec
# ===========================================================================


def test_latency_helper_computes_seconds():
    from backend.crm.service import latency_to_resolution_sec

    latency = latency_to_resolution_sec(
        "2026-05-20T00:00:00Z",
        "2026-05-20T01:00:00Z",
    )
    assert latency == 3600.0


def test_latency_helper_none_on_unparseable():
    from backend.crm.service import latency_to_resolution_sec

    assert latency_to_resolution_sec("", "2026-05-20T00:00:00Z") is None
    assert latency_to_resolution_sec("not-a-date", "also-bad") is None


def test_latency_helper_none_on_negative_delta():
    from backend.crm.service import latency_to_resolution_sec

    # close precedes creation — clock skew / data error, not a real latency.
    assert (
        latency_to_resolution_sec(
            "2026-05-20T05:00:00Z",
            "2026-05-20T01:00:00Z",
        )
        is None
    )


def test_advance_to_terminal_surfaces_latency(tmp_path, monkeypatch):
    """A closed_won advance returns latency_to_resolution_sec on the result."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_lat")] = {
        "opportunity_id": "op_lat",
        "stage": "proposal",
        "deal_size_usd": 500.0,
        "created_at": "2026-05-19T00:00:00Z",
    }
    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_lat",
            target_stage="closed_won",
            won_amount_usd=500.0,
        )
    )
    assert result.status == "advanced"
    # created 2026-05-19, closed "now" — latency is a positive number of secs.
    assert result.latency_to_resolution_sec is not None
    assert result.latency_to_resolution_sec > 0


def test_non_terminal_advance_has_no_latency(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_nt")] = {
        "opportunity_id": "op_nt",
        "stage": "new",
        "created_at": "2026-05-19T00:00:00Z",
    }
    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_nt",
            target_stage="qualified",
        )
    )
    assert result.status == "advanced"
    assert result.latency_to_resolution_sec is None


def test_latency_lands_in_audit_event(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_aud")] = {
        "opportunity_id": "op_aud",
        "stage": "negotiation",
        "created_at": "2026-05-18T00:00:00Z",
        "deal_size_usd": 900.0,
    }
    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_aud",
            target_stage="closed_lost",
            lost_reason="ghosted",
        )
    )
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    advanced = [
        r
        for r in lines
        if r.get("action") == "advance_opportunity" and r.get("status") == "completed"
    ]
    assert advanced
    # output_payload is hashed in the audit event, so we can only assert the
    # event exists; the latency value rides in the (hashed) output_payload.
    assert advanced[-1]["output_hash"]


# ===========================================================================
# Feature 2 — estimated_close_probability
# ===========================================================================


def _seed_opportunities(tables, rows):
    for r in rows:
        tables["_opportunities_table"].items[("opportunity_id", r["opportunity_id"])] = r


def test_close_probability_per_industry(monkeypatch):
    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "industry": "plumbing", "stage": "closed_won"},
            {"opportunity_id": "o2", "industry": "plumbing", "stage": "closed_lost"},
            {"opportunity_id": "o3", "industry": "plumbing", "stage": "closed_won"},
            {"opportunity_id": "o4", "industry": "plumbing", "stage": "qualified"},
            {"opportunity_id": "o5", "industry": "hvac", "stage": "closed_won"},
        ],
    )
    from backend.crm.service import estimated_close_probability

    metrics = estimated_close_probability()

    plumbing = metrics["by_industry"]["plumbing"]
    assert plumbing["total"] == 4
    assert plumbing["won"] == 2
    assert plumbing["lost"] == 1
    assert plumbing["open"] == 1
    assert plumbing["close_probability"] == 0.5

    hvac = metrics["by_industry"]["hvac"]
    assert hvac["close_probability"] == 1.0
    assert metrics["sample_size"] == 5


def test_close_probability_per_stage(monkeypatch):
    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "industry": "x", "stage": "closed_won"},
            {"opportunity_id": "o2", "industry": "x", "stage": "closed_won"},
            {"opportunity_id": "o3", "industry": "x", "stage": "proposal"},
        ],
    )
    from backend.crm.service import estimated_close_probability

    metrics = estimated_close_probability()
    assert metrics["by_stage"]["closed_won"]["close_probability"] == 1.0
    assert metrics["by_stage"]["proposal"]["close_probability"] == 0.0


def test_close_probability_retainer_counts_as_won(monkeypatch):
    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "industry": "y", "stage": "closed_won_retainer"},
            {"opportunity_id": "o2", "industry": "y", "stage": "closed_lost"},
        ],
    )
    from backend.crm.service import estimated_close_probability

    metrics = estimated_close_probability()
    assert metrics["by_industry"]["y"]["won"] == 1
    assert metrics["by_industry"]["y"]["close_probability"] == 0.5


def test_close_probability_unspecified_industry_bucket(monkeypatch):
    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "stage": "closed_won"},  # no industry
        ],
    )
    from backend.crm.service import estimated_close_probability

    metrics = estimated_close_probability()
    assert "(unspecified)" in metrics["by_industry"]


def test_close_probability_empty_table(monkeypatch):
    _patch_tables(monkeypatch)
    from backend.crm.service import estimated_close_probability

    metrics = estimated_close_probability()
    assert metrics["sample_size"] == 0
    assert metrics["by_industry"] == {}
    assert metrics["ddb_error"] is None


def test_close_probability_degrades_on_ddb_error(monkeypatch):
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].fail_get = True  # scan also raises

    class _BoomScan:
        def scan(self, **kw):
            raise RuntimeError("ddb down")

    import backend.crm.persistence as p

    monkeypatch.setattr(p, "_opportunities_table", lambda: _BoomScan())
    from backend.crm.service import estimated_close_probability

    metrics = estimated_close_probability()
    assert metrics["ddb_error"] is not None
    assert metrics["sample_size"] == 0


# ===========================================================================
# Feature 3 — token_cost_by_industry
# ===========================================================================


def test_token_cost_rollup_per_industry(monkeypatch):
    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {
                "opportunity_id": "o1",
                "industry": "plumbing",
                "stage": "new",
                "token_cost_usd": 0.10,
            },
            {
                "opportunity_id": "o2",
                "industry": "plumbing",
                "stage": "new",
                "token_cost_usd": 0.30,
            },
            {"opportunity_id": "o3", "industry": "hvac", "stage": "new", "token_cost_usd": 0.05},
        ],
    )
    from backend.crm.service import token_cost_by_industry

    metrics = token_cost_by_industry()

    plumbing = metrics["by_industry"]["plumbing"]
    assert plumbing["opportunity_count"] == 2
    assert plumbing["total_token_cost_usd"] == 0.40
    assert plumbing["mean_token_cost_usd"] == 0.20

    hvac = metrics["by_industry"]["hvac"]
    assert hvac["total_token_cost_usd"] == 0.05
    assert metrics["grand_total_token_cost_usd"] == 0.45
    assert metrics["sample_size"] == 3


def test_token_cost_zero_when_unset(monkeypatch):
    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "industry": "x", "stage": "new"},  # no cost
        ],
    )
    from backend.crm.service import token_cost_by_industry

    metrics = token_cost_by_industry()
    assert metrics["by_industry"]["x"]["total_token_cost_usd"] == 0.0
    assert metrics["grand_total_token_cost_usd"] == 0.0


def test_token_cost_empty_table(monkeypatch):
    _patch_tables(monkeypatch)
    from backend.crm.service import token_cost_by_industry

    metrics = token_cost_by_industry()
    assert metrics["sample_size"] == 0
    assert metrics["by_industry"] == {}


# ===========================================================================
# GET /crm/metrics/* routes
# ===========================================================================


def test_close_probability_route(monkeypatch):
    from fastapi.testclient import TestClient

    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "industry": "roofing", "stage": "closed_won"},
        ],
    )
    from backend.crm.app import app

    r = TestClient(app).get("/crm/metrics/close-probability")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["by_industry"]["roofing"]["close_probability"] == 1.0


def test_token_cost_route(monkeypatch):
    from fastapi.testclient import TestClient

    tables = _patch_tables(monkeypatch)
    _seed_opportunities(
        tables,
        [
            {"opportunity_id": "o1", "industry": "roofing", "stage": "new", "token_cost_usd": 0.25},
        ],
    )
    from backend.crm.app import app

    r = TestClient(app).get("/crm/metrics/token-cost")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["by_industry"]["roofing"]["total_token_cost_usd"] == 0.25
    assert body["grand_total_token_cost_usd"] == 0.25
