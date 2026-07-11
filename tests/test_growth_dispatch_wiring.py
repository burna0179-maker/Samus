"""End-to-end wiring of the growth-enrichment capabilities into /work.

Proves each new action is reachable through the governed envelope dispatch
(capability-checked) and returns its handler output. Everything here is
dormant: no LLM spend (handlers default use_llm=False; aio_probe is flag-gated
off), no live posting (dispatch is DRY-RUN), no payouts.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.common.capabilities import SERVICE_CAPABILITIES, check_capability


def _client(service: str) -> TestClient:
    if service == "seo":
        from backend.seo.app import create_app
    elif service == "outreach":
        from backend.outreach.app import create_app
    elif service == "crm":
        from backend.crm.app import create_app
    else:  # pragma: no cover
        raise ValueError(service)
    return TestClient(create_app())


def _work(client: TestClient, action: str, payload: dict) -> dict:
    r = client.post(
        "/work",
        json={"task_id": "t", "payload": payload, "metadata": {"action": action}},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# capability registration
# ---------------------------------------------------------------------------


def test_new_capabilities_registered():
    assert {"geo_format", "aio_analyze", "aio_probe"} <= SERVICE_CAPABILITIES["seo"]
    assert {
        "repurpose_blog_post",
        "plan_social_calendar",
        "dispatch_social_calendar",
        "plan_nurture",
    } <= SERVICE_CAPABILITIES["outreach"]
    assert {
        "generate_case_study",
        "build_proof_wall",
        "referral_code",
        "referral_record",
        "referral_qualify",
    } <= SERVICE_CAPABILITIES["crm"]
    # check_capability passes for a new one, denies an unknown one.
    check_capability("seo", "geo_format")  # no raise
    with pytest.raises(Exception):
        check_capability("seo", "not_a_capability")


# ---------------------------------------------------------------------------
# seo: geo + visibility
# ---------------------------------------------------------------------------


def test_work_geo_format():
    c = _client("seo")
    out = _work(
        c,
        "geo_format",
        {
            "drafts": {"h1": "Emergency Plumbing", "body_intro": "Burst pipes can't wait. We arrive same day with fixed pricing.", "body_main": "We handle drains and heaters."},
            "keywords": ["emergency plumbing", "drain cleaning"],
        },
    )
    assert out["golden_answer"]
    assert len(out["faq"]) >= 5
    assert out["faq_schema"]["@type"] == "FAQPage"
    assert out["used_llm"] is False


def test_work_aio_analyze_pure():
    c = _client("seo")
    out = _work(
        c,
        "aio_analyze",
        {
            "answers": [{"platform": "claude", "query": "best tool", "text": "Hustleforge and Apollo. https://apollo.io"}],
            "brand_terms": ["Hustleforge"],
            "competitor_terms": ["Apollo"],
        },
    )
    assert out["sov"]["sample_n"] == 1
    assert out["sov"]["citation_rate"] == 1.0
    assert out["probes"][0]["brand_cited"] is True


def test_work_aio_probe_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SAMUS_VISIBILITY_PROBE_ENABLED", raising=False)
    c = _client("seo")
    out = _work(c, "aio_probe", {"questions": ["q"], "brand_terms": ["Hustleforge"]})
    assert out["enabled"] is False
    assert out["reason"] == "probe_disabled"


# ---------------------------------------------------------------------------
# outreach: social + nurture
# ---------------------------------------------------------------------------


def test_work_repurpose_blog_post():
    c = _client("outreach")
    out = _work(c, "repurpose_blog_post", {"title": "The 44% rule", "summary": "AI cites the first 30%.", "key_points": ["lead with the answer"]})
    assert len(out["assets"]) == 8
    assert out["used_llm"] is False


def test_work_plan_social_calendar():
    c = _client("outreach")
    out = _work(c, "plan_social_calendar", {"month": 1, "cluster": "AI prospecting"})
    assert out["post_count"] == 72
    assert out["mix"]["convert"] <= 0.15


def test_work_dispatch_social_calendar_dry_run_empty():
    c = _client("outreach")
    out = _work(c, "dispatch_social_calendar", {"posts": []})
    assert out["dispatched"] == 0


def test_work_plan_nurture():
    c = _client("outreach")
    out = _work(
        c,
        "plan_nurture",
        {
            "sequence_id": "welcome",
            "enrollment": {"prospect_id": "p1", "sequence_id": "welcome", "started_at": "2026-06-01T00:00:00+00:00"},
            "now": "2026-06-01T01:00:00+00:00",
        },
    )
    assert out["action"] == "send"
    assert out["message"]["dry_run"] is True
    assert out["message"]["step"] == 1


def test_work_plan_nurture_unknown_sequence():
    c = _client("outreach")
    out = _work(c, "plan_nurture", {"sequence_id": "nope"})
    assert out["error"].startswith("unknown_sequence")


# ---------------------------------------------------------------------------
# crm: proof + referral
# ---------------------------------------------------------------------------


def test_work_generate_case_study():
    c = _client("crm")
    out = _work(
        c,
        "generate_case_study",
        {"company": "RiverCity", "results": ["3x booked jobs"], "challenge": "missed leads"},
    )
    assert "RiverCity" in out["title"]
    assert out["schema_jsonld"]["@type"] == "Article"
    assert out["used_llm"] is False


def test_work_build_proof_wall():
    c = _client("crm")
    out = _work(c, "build_proof_wall", {"points": [{"company": "A", "result": "2x", "industry": "SaaS"}]})
    assert out["count"] == 1
    assert out["industries"] == ["SaaS"]


def test_work_referral_code():
    c = _client("crm")
    out = _work(c, "referral_code", {"referrer_id": "cust_42"})
    assert len(out["code"]) == 8
    assert out["link"].startswith("https://hustleforge.ai/?ref=")
