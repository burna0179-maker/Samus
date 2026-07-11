"""Tests for backend.scaffold.templates."""
from __future__ import annotations

import pytest


@pytest.fixture
def sample_payload():
    return {
        "asset_type": "proposal_pack",
        "title": "Ops Pilot",
        "client": "Acme",
        "brand_voice": "direct",
        "goals": ["reduce manual ops"],
        "positioning": {
            "problem": "manual ops",
            "mechanism": "automation-first orchestration",
            "outcome": "faster throughput",
        },
        "offer": {
            "headline": "Pilot: eliminate manual ops",
            "mechanism": "automation",
            "outcome": "faster throughput",
            "price_anchor": "$500-$5,000 depending on scope",
        },
        "sequence": [
            {"step": 1, "message": "opening"},
            {"step": 2, "message": "mechanism"},
            {"step": 3, "message": "outcome"},
            {"step": 4, "message": "cta"},
        ],
    }


@pytest.mark.parametrize("asset_type,header", [
    ("proposal_pack", "Proposal Pack"),
    ("implementation_plan", "Implementation Plan"),
    ("operating_brief", "Operating Brief"),
    ("campaign_brief", "Campaign Brief"),
])
def test_render_template_known_types(sample_payload, asset_type, header):
    from backend.scaffold.templates import render_template

    sample_payload["asset_type"] = asset_type
    doc = render_template(asset_type, sample_payload)
    assert header in doc
    assert sample_payload["title"] in doc
    assert sample_payload["client"] in doc


def test_render_template_unknown_type(sample_payload):
    from backend.scaffold.templates import render_template

    doc = render_template("nonsense_type", sample_payload)
    assert "unknown asset_type" in doc
