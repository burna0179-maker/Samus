"""Tests for backend.voice.close_handoff — HOTL close -> checkout draft queue."""
from __future__ import annotations

import json

from backend.voice import close_handoff as ch


def _closed_call(*, product="SEO Audit", price=149, transcript="", cid="c1",
                 contact_email=None, prospect_id="pr_9"):
    ls = {
        "company": "Dave's Diner",
        "pain_points": ["no working website"],
        "intent_score": 80,
        "recommended_action": "book_call",
        "validated_product_name": product,
        "validated_product_price": price,
    }
    if contact_email is not None:
        ls["contact_email"] = contact_email
    return {
        "id": cid,
        "transcript": transcript,
        "analysis": {"structuredData": {"lead_summary": ls}},
        "metadata": {"prospect_id": prospect_id},
    }


def test_detect_close_builds_pending_with_stripe_link():
    call = _closed_call(transcript="User: email me at jane@dinerco.com\nAI: got it.")
    p = ch.detect_close(call)
    assert p is not None
    assert p.product_name == "SEO Audit"
    assert p.email == "jane@dinerco.com"
    assert p.email_confidence == "transcript"
    assert p.needs_email is False
    # a real, attributed Stripe checkout link. The ref carries the out_
    # namespace prefix (see backend.finance.outreach_attribution.OUT_PREFIX /
    # flyer.buy_url) so the webhook can attribute the sale to the prospect.
    assert "buy.stripe.com" in p.checkout_url
    assert "client_reference_id=out_pr_9" in p.checkout_url
    assert p.checkout_url in p.body
    assert p.subject and p.body


def test_no_close_when_validated_product_null():
    call = _closed_call(product="")           # null/empty => not a close
    assert ch.detect_close(call) is None


def test_missing_email_flags_needs_email():
    call = _closed_call(transcript="User: I'll think about it.\nAI: sure.")
    p = ch.detect_close(call)
    assert p is not None
    assert p.email == "" and p.needs_email is True
    assert p.email_confidence == "missing"


def test_structured_contact_email_preferred():
    call = _closed_call(contact_email="owner@shop.com",
                        transcript="User: also cc random@else.com")
    p = ch.detect_close(call)
    assert p.email == "owner@shop.com"
    assert p.email_confidence == "structured"


def test_hustleforge_email_in_transcript_ignored():
    call = _closed_call(transcript="AI: this is morgan@hustleforge.tech calling")
    p = ch.detect_close(call)
    assert p.email == "" and p.needs_email is True   # Morgan's own address skipped


def test_queue_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(ch.storage, "root", lambda: tmp_path)
    calls = [_closed_call(transcript="User: me@co.com", cid="cX")]
    first = ch.queue_close_handoffs(calls)
    assert len(first) == 1
    f = tmp_path / "voice" / "close_handoffs" / "pending_cX.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["product_name"] == "SEO Audit" and "buy.stripe.com" in data["checkout_url"]
    # re-run -> already queued -> skipped
    second = ch.queue_close_handoffs(calls)
    assert second == []
    assert len(list((tmp_path / "voice" / "close_handoffs").glob("pending_*.json"))) == 1


def test_queue_skips_non_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(ch.storage, "root", lambda: tmp_path)
    calls = [_closed_call(product="", cid="nope")]
    assert ch.queue_close_handoffs(calls) == []
    assert not (tmp_path / "voice" / "close_handoffs").exists() or \
        not list((tmp_path / "voice" / "close_handoffs").glob("pending_*.json"))
