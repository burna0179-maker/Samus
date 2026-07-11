"""Campaign template + graph validation tests (deliverable §11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.campaigns.graph import CampaignGraph, GraphError, evaluate_condition
from backend.campaigns.models import CampaignInstance
from backend.campaigns.templates import (
    TemplateError,
    load_instance,
    load_template,
    parse_template,
)

_ROOT = Path(__file__).resolve().parents[1]
_SCHOOL = _ROOT / "backend" / "campaigns" / "templates" / "school_enrollment_campaign.yaml"
_CONQUERORS = _ROOT / "clients" / "sample_school" / "campaign.yaml"


def _min_template(**over) -> dict:
    data = {
        "template_id": "t",
        "vertical": "test",
        "kpis": [{"key": "page_views"}],
        "nodes": [
            {
                "id": "a",
                "type": "seo_audit",
                "target_workcell": "seo",
                "capability": "audit_and_report",
            }
        ],
        "edges": [],
    }
    data.update(over)
    return data


# --- loading + validating a template --------------------------------------


def test_loads_and_validates_a_template():
    tpl = parse_template(_min_template())
    assert tpl.template_id == "t"
    assert tpl.node_map()["a"].capability == "audit_and_report"


def test_loads_school_enrollment_template_from_yaml():
    tpl = load_template(_SCHOOL)
    assert tpl.template_id == "school_enrollment_campaign"
    assert tpl.vertical == "education"
    ids = {n.id for n in tpl.nodes}
    # the core user-specified graph nodes are all present
    for required in (
        "intake_assets",
        "audit_website",
        "build_enrollment_funnel",
        "publish_social_content",
        "collect_campaign_metrics",
        "weekly_report",
    ):
        assert required in ids
    # every node routes to a real workcell/capability pair (load would raise
    # otherwise) and the graph is acyclic.
    CampaignGraph(tpl).topological_order()


# --- validating graph nodes and edges -------------------------------------


def test_rejects_duplicate_node_ids():
    data = _min_template(
        nodes=[
            {
                "id": "a",
                "type": "seo_audit",
                "target_workcell": "seo",
                "capability": "audit_and_report",
            },
            {
                "id": "a",
                "type": "seo_audit",
                "target_workcell": "seo",
                "capability": "audit_and_report",
            },
        ]
    )
    with pytest.raises(TemplateError, match="duplicate node ids"):
        parse_template(data)


def test_rejects_edge_to_unknown_node():
    data = _min_template(edges=[{"from": "a", "to": "ghost"}])
    with pytest.raises(TemplateError, match="unknown node"):
        parse_template(data)


def test_detects_cycles():
    tpl = parse_template(
        _min_template(
            nodes=[
                {
                    "id": "a",
                    "type": "seo_audit",
                    "target_workcell": "seo",
                    "capability": "audit_and_report",
                },
                {
                    "id": "b",
                    "type": "metrics_collection",
                    "target_workcell": "campaigns",
                    "capability": "update_kpis",
                },
            ],
            edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
        )
    )
    with pytest.raises(GraphError, match="cycle"):
        CampaignGraph(tpl)


# --- preventing invalid workcell/capability pairs -------------------------


def test_rejects_unknown_target_workcell():
    data = _min_template(
        nodes=[{"id": "a", "type": "seo_audit", "target_workcell": "nope", "capability": "x"}]
    )
    with pytest.raises(TemplateError, match="unknown target workcell"):
        parse_template(data)


def test_rejects_capability_not_exposed_by_workcell():
    data = _min_template(
        nodes=[
            {"id": "a", "type": "seo_audit", "target_workcell": "seo", "capability": "send_message"}
        ]
    )
    with pytest.raises(TemplateError, match="does not expose capability"):
        parse_template(data)


def test_rejects_node_referencing_undeclared_kpi():
    data = _min_template(
        nodes=[
            {
                "id": "a",
                "type": "seo_audit",
                "target_workcell": "seo",
                "capability": "audit_and_report",
                "kpis": ["undeclared_kpi"],
            }
        ]
    )
    with pytest.raises(TemplateError, match="undeclared KPI"):
        parse_template(data)


# --- conditional branching -------------------------------------------------


def test_condition_evaluation():
    ctx = {"kpi": {"application_starts": 12}}
    assert evaluate_condition({"field": "kpi.application_starts", "op": "gte", "value": 10}, ctx)
    assert not evaluate_condition({"field": "kpi.application_starts", "op": "lt", "value": 10}, ctx)
    # no condition is always true; unknown operator fails closed.
    assert evaluate_condition(None, ctx)
    assert not evaluate_condition({"field": "kpi.x", "op": "bogus"}, ctx)


# --- creating a Sample School instance ----------------------


def test_loads_conquerors_instance_from_yaml():
    inst = load_instance(_CONQUERORS)
    assert isinstance(inst, CampaignInstance)
    assert inst.client_id == "sample_school"
    # Instance pivoted 2026-07-10 from accelerated -> phased maintenance
    # after the client counter-offered on the $3,550 accelerated proposal.
    # See clients/sample_school/campaign.yaml header comment.
    assert inst.template_id == "school_phased_maintenance"
    assert inst.campaign_id == "sample_school_phased_2026"
    # Phased posture does NOT drive a social publish cadence — the field
    # is intentionally absent from inputs on this pivot.
    assert "social_channels" not in inst.inputs
    # KPI targets downgraded to modest ongoing metrics.
    assert inst.kpi_targets["page_views"] == 500
    assert inst.kpi_targets["tour_requests"] == 5
    assert "application_starts" not in inst.kpi_targets
