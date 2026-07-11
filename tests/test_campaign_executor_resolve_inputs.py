"""Unit tests for ``executor.resolve_inputs`` (input_mapping projection).

Covers the recursive-dict resolution fix: nested ``$``-refs inside a dict
``input_mapping`` value must resolve against the run context instead of
passing through as literal unresolved strings, while flat-string/literal
behaviour stays byte-identical to before the fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.campaigns.executor import resolve_inputs
from backend.campaigns.models import CampaignNode, CampaignRun
from backend.campaigns.templates import load_template
from backend.scaffold.models import ScaffoldRequest

_ROOT = Path(__file__).resolve().parents[1]
_SCHOOL_ENROLLMENT = (
    _ROOT / "backend" / "campaigns" / "templates" / "school_enrollment_campaign.yaml"
)
_SCHOOL_PHASED = (
    _ROOT / "backend" / "campaigns" / "templates" / "school_phased_maintenance.yaml"
)


def _run(**context_over) -> CampaignRun:
    run = CampaignRun(campaign_id="c1", client_id="acme", template_id="t1")
    run.context = {
        "inputs": {
            "school_name": "Acme Academy",
            "social_posting_cadence": "3/week",
            "social_channels": ["facebook", "instagram"],
        },
        **context_over,
    }
    return run


# --- (a) existing flat-string / literal resolution is unchanged -----------


def test_flat_string_ref_and_literal_resolve_as_before():
    node = CampaignNode(
        id="n1", type="content_generation", target_workcell="scaffold",
        capability="generate_assets",
        input_mapping={
            "school_name": "$inputs.school_name",
            "topic": "academic outcomes",
        },
    )
    payload = resolve_inputs(node, _run())
    assert payload["school_name"] == "Acme Academy"
    assert payload["topic"] == "academic outcomes"
    # injected fields still present
    assert payload["campaign_id"] == "c1"
    assert payload["client_id"] == "acme"
    assert payload["node_id"] == "n1"


def test_missing_ref_resolves_to_none_as_before():
    node = CampaignNode(
        id="n1", type="content_generation", target_workcell="scaffold",
        capability="generate_assets",
        input_mapping={"missing": "$inputs.does_not_exist"},
    )
    payload = resolve_inputs(node, _run())
    assert payload["missing"] is None


# --- (b) nested-dict input_mapping value with $-refs resolves correctly ---


def test_nested_dict_value_resolves_dollar_refs():
    node = CampaignNode(
        id="n2", type="content_generation", target_workcell="scaffold",
        capability="generate_assets",
        input_mapping={
            "inputs": {
                "cadence": "$inputs.social_posting_cadence",
                "channels": "$inputs.social_channels",
            }
        },
    )
    payload = resolve_inputs(node, _run())
    assert payload["inputs"] == {
        "cadence": "3/week",
        "channels": ["facebook", "instagram"],
    }


# --- (c) nested dict mixing literal + $-ref values -------------------------


def test_nested_dict_mixes_literal_and_ref_values():
    node = CampaignNode(
        id="n3", type="content_generation", target_workcell="scaffold",
        capability="generate_assets",
        input_mapping={
            "inputs": {
                "topic": "academic outcomes",
                "school_name": "$inputs.school_name",
            }
        },
    )
    payload = resolve_inputs(node, _run())
    assert payload["inputs"] == {
        "topic": "academic outcomes",
        "school_name": "Acme Academy",
    }


def test_doubly_nested_dict_resolves_all_levels():
    node = CampaignNode(
        id="n4", type="content_generation", target_workcell="scaffold",
        capability="generate_assets",
        input_mapping={
            "inputs": {
                "meta": {"school_name": "$inputs.school_name", "literal": "x"},
            }
        },
    )
    payload = resolve_inputs(node, _run())
    assert payload["inputs"] == {"meta": {"school_name": "Acme Academy", "literal": "x"}}


# --- verification: every content_generation->scaffold node in both school --
# --- templates now produces a payload that validates as ScaffoldRequest ----


def _content_generation_scaffold_nodes(template) -> list[CampaignNode]:
    return [
        n for n in template.nodes
        if n.type == "content_generation" and n.target_workcell == "scaffold"
    ]


@pytest.mark.parametrize("template_path", [_SCHOOL_ENROLLMENT, _SCHOOL_PHASED])
def test_every_content_generation_node_produces_valid_scaffold_request(template_path):
    template = load_template(template_path)
    nodes = _content_generation_scaffold_nodes(template)
    assert nodes, f"expected at least one content_generation/scaffold node in {template_path.name}"

    run = _run()
    for node in nodes:
        payload = resolve_inputs(node, run)
        # ScaffoldRequest ignores unknown top-level keys (extra="ignore" default)
        # so we must assert the REQUIRED fields actually made it into the
        # payload — not just that validation doesn't raise.
        req = ScaffoldRequest(**payload)
        assert req.asset_type == "campaign_brief"
        assert req.title
        assert req.client == "Acme Academy"
        assert req.brand_voice
        assert req.offer
