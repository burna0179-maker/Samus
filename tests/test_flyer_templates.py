"""Tests for backend.outreach.flyer_templates — the reusable flyer library."""

from __future__ import annotations

import json

import pytest

from backend.outreach import flyer_templates as ft
from backend.outreach.flyer import Offer


def _featured_offer():
    return Offer(
        sku_id="service_workflow_rescue",
        label="Stop doing that manual task by Friday",
        price_usd=500.0,
        payment_link="https://buy.stripe.com/live",
        kind="featured",
        headline="Stop doing that manual task by Friday",
        pitch="Pick one repetitive task and we automate it in 48 hours.",
        cta_label="Start My 48-Hour Automation",
        assurance="Limited to 3 builds per week.",
        bullets=("Audit + blueprint", "Build + deploy", "Handoff + walkthrough"),
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ft.storage, "root", lambda: tmp_path)
    return tmp_path


def test_save_template_writes_file_and_manifest(store):
    r = ft.save_template(_featured_offer(), sample_company="Chavez Web Design")
    assert r is not None and r.changed is True
    assert r.template_id == "featured_service_workflow_rescue"
    path = store / "marketing" / "flyer_templates" / "featured_service_workflow_rescue.html"
    assert path.exists()
    manifest = store / "marketing" / "flyer_templates" / "manifest.jsonl"
    rows = [json.loads(l) for l in manifest.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["sku_id"] == "service_workflow_rescue"
    assert rows[0]["sample_company"] == "Chavez Web Design"


def test_featured_template_contains_merge_fields(store):
    ft.save_template(_featured_offer())
    html = ft.load_template("featured_service_workflow_rescue")
    assert html is not None
    assert "{{first_name}}" in html  # greeting merge field
    assert "{{buy_now_url}}" in html  # CTA link merge field
    # the fixed offer copy is baked in
    assert "Start My 48-Hour Automation" in html
    assert "Stop doing that manual task by Friday" in html


def test_matched_template_contains_company_merge_field(store):
    matched = Offer(
        sku_id="seo_audit",
        label="Full Website & Security Audit",
        price_usd=149.0,
        payment_link="https://buy.stripe.com/live",
        why="the fixes ranked by impact",
        kind="matched",
    )
    ft.save_template(matched)
    html = ft.load_template("matched_seo_audit")
    assert html is not None
    assert "{{company}}" in html  # "When we looked at {{company}}"
    assert "{{buy_now_url}}" in html


def test_save_template_dedupes_unchanged(store):
    r1 = ft.save_template(_featured_offer())
    r2 = ft.save_template(_featured_offer())
    assert r1.changed is True
    assert r2 is not None and r2.changed is False
    # only one manifest version for identical content
    manifest = store / "marketing" / "flyer_templates" / "manifest.jsonl"
    rows = [l for l in manifest.read_text().splitlines() if l.strip()]
    assert len(rows) == 1


def test_new_version_on_content_change(store):
    ft.save_template(_featured_offer())
    changed = _featured_offer()
    changed.cta_label = "Book My 48-Hour Build"  # copy change -> new version
    r = ft.save_template(changed)
    assert r.changed is True
    manifest = store / "marketing" / "flyer_templates" / "manifest.jsonl"
    rows = [l for l in manifest.read_text().splitlines() if l.strip()]
    assert len(rows) == 2


def test_list_templates(store):
    ft.save_template(_featured_offer())
    assert "featured_service_workflow_rescue" in ft.list_templates()
