"""Tests for backend.finance.outreach_attribution — flyer -> purchase credit."""
from __future__ import annotations

import json

import pytest

from backend.finance import outreach_attribution as oa


@pytest.fixture
def conv_log(tmp_path, monkeypatch):
    p = tmp_path / "outreach_conversions.jsonl"
    monkeypatch.setenv("SAMUS_OUTREACH_CONVERSIONS_LOG", str(p))
    return p


def test_prospect_id_from_ref():
    assert oa.prospect_id_from_ref("out_pr_42") == "pr_42"
    assert oa.prospect_id_from_ref("out_apollo_ChIJ_x") == "apollo_ChIJ_x"
    assert oa.prospect_id_from_ref("op_123") == ""      # opportunity ref, not outreach
    assert oa.prospect_id_from_ref("upsell_9") == ""
    assert oa.prospect_id_from_ref("") == ""
    assert oa.prospect_id_from_ref("out_") == ""        # empty payload


def test_record_conversion_writes_record(conv_log):
    rec = oa.record_conversion(
        ref="out_pr_42", email="o@x.com", amount_usd=500.0, currency="USD",
        offer_code="workflow_rescue", event_id="evt_1", received_at="2026-07-02T00:00:00Z",
    )
    assert rec is not None
    assert rec.prospect_id == "pr_42"
    assert rec.amount_usd == 500.0
    assert rec.currency == "usd"
    lines = [l for l in conv_log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["prospect_id"] == "pr_42"


def test_record_conversion_ignores_non_outreach_ref(conv_log):
    assert oa.record_conversion(ref="op_123", amount_usd=149.0, event_id="e") is None
    assert not conv_log.exists() or conv_log.read_text().strip() == ""


def test_record_conversion_idempotent_by_event(conv_log):
    a = oa.record_conversion(ref="out_pr_1", amount_usd=149.0, event_id="evt_dup")
    b = oa.record_conversion(ref="out_pr_1", amount_usd=149.0, event_id="evt_dup")
    assert a is not None and b is None  # second is a no-op (retry)
    assert len([l for l in conv_log.read_text().splitlines() if l.strip()]) == 1


def test_load_conversions_roundtrip(conv_log):
    oa.record_conversion(ref="out_pr_1", amount_usd=149.0, event_id="e1")
    oa.record_conversion(ref="out_pr_2", amount_usd=500.0, event_id="e2")
    got = oa.load_conversions()
    assert {c.prospect_id for c in got} == {"pr_1", "pr_2"}
