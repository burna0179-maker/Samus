"""Buying-signal email route — classifier, gated enrollment, dormant dispatch."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.common.config as config_mod
import backend.outreach.buying_signal_route as bsr
import backend.outreach.service as outreach_service


def _lead(**kw):
    base = dict(intent_score=None, recommended_action=None, tier=None,
                company="Acme Plumbing", contact_offered="")
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_get_settings(
    *, enabled: bool, threshold: int = 70, website_enabled: bool = False,
):
    """A get_settings stand-in carrying a no-op ``cache_clear`` — the repo's
    autouse conftest calls ``reload_settings()`` (-> ``get_settings.cache_clear()``)
    in teardown, which would otherwise blow up on a bare lambda."""
    def _gs():
        return SimpleNamespace(
            outreach_buying_signal_route_enabled=enabled,
            outreach_buying_signal_intent_threshold=threshold,
            outreach_website_form_warm_enroll_enabled=website_enabled,
        )
    _gs.cache_clear = lambda: None
    return _gs


def _stored_lead(**kw):
    """Minimal StoredLead-shaped object for website-form enrollment tests."""
    base = dict(
        lead_id="lead_xyz1",
        email="prospect@example.com",
        company="Acme Realty",
        service_interest=["seo_audit"],
        monthly_budget="$500-$2000",
        timeline="asap",
        pain_points="Our current SEO is broken, we need a comprehensive audit "
                    "and fix pass to reclaim the local search rankings we lost "
                    "over the last quarter.",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def armed(monkeypatch, tmp_path):
    """Flag ON + isolated store dir."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(enabled=True))
    return tmp_path


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def test_is_buying_signal_book_call_qualifies_regardless_of_score():
    assert bsr.is_buying_signal(_lead(recommended_action="book_call", intent_score=5))


def test_is_buying_signal_high_tier_qualifies():
    assert bsr.is_buying_signal(_lead(tier="high"))
    assert bsr.is_buying_signal(_lead(tier="priority"))


def test_is_buying_signal_score_threshold():
    assert bsr.is_buying_signal(_lead(intent_score=70), intent_threshold=70)
    assert not bsr.is_buying_signal(_lead(intent_score=69), intent_threshold=70)


def test_is_buying_signal_disqualify_never_qualifies():
    # Even a high score loses to a hard disqualify.
    assert not bsr.is_buying_signal(
        _lead(recommended_action="disqualify", intent_score=99, tier="high")
    )


def test_is_buying_signal_none_and_bool_score():
    assert not bsr.is_buying_signal(None)
    # bool is a subclass of int — must not count True as a score >= threshold.
    assert not bsr.is_buying_signal(_lead(intent_score=True), intent_threshold=70)


# ---------------------------------------------------------------------------
# Enrollment gating
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Website-form enrollment (site-owned funnel warm-lead capture)
# ---------------------------------------------------------------------------

def test_website_form_no_op_when_flag_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "get_settings",
        _fake_get_settings(enabled=False, website_enabled=False),
    )
    out = bsr.maybe_enroll_buying_signal_from_website_form(
        stored_lead=_stored_lead(), now_iso="2026-07-03T12:00:00Z",
    )
    assert out["enrolled"] is False
    assert out["reason"] == "route_disabled"
    assert bsr._read() == []


def test_website_form_skips_low_signal_leads(monkeypatch, tmp_path):
    """Empty or 'not_sure'-only service_interest -> no enrollment."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "get_settings",
        _fake_get_settings(enabled=False, website_enabled=True),
    )
    out_empty = bsr.maybe_enroll_buying_signal_from_website_form(
        stored_lead=_stored_lead(service_interest=[]),
        now_iso="2026-07-03T12:00:00Z",
    )
    assert out_empty["reason"] == "no_declared_interest"
    out_notsure = bsr.maybe_enroll_buying_signal_from_website_form(
        stored_lead=_stored_lead(service_interest=["not_sure"]),
        now_iso="2026-07-03T12:00:00Z",
    )
    assert out_notsure["reason"] == "no_declared_interest"
    assert bsr._read() == []


def test_website_form_enrolls_high_intent_and_uses_own_gate(monkeypatch, tmp_path):
    """The website-form flag alone is sufficient — the voice-path flag stays
    OFF and the enrollment still lands via enabled_override=True."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "get_settings",
        _fake_get_settings(enabled=False, website_enabled=True),
    )
    out = bsr.maybe_enroll_buying_signal_from_website_form(
        stored_lead=_stored_lead(),  # SEO audit + $500-$2000 + asap + long pain
        now_iso="2026-07-03T12:00:00Z",
    )
    assert out["enrolled"] is True
    records = bsr._read()
    assert len(records) == 1
    rec = records[0]
    assert rec["prospect_id"] == "lead_lead_xyz1"
    assert rec["source"] == "website_form"
    assert rec["email"] == "prospect@example.com"
    # Rubric: 60 (declared interest) + 15 (asap) + 15 (budget) + 5 (long pain)
    # = 95 -> tier "high", recommended_action "book_call".
    assert rec["intent_score"] == 95
    assert rec["tier"] == "high"


def test_enroll_no_op_when_flag_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(enabled=False))
    out = bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=_lead(recommended_action="book_call"),
        now_iso="2026-06-30T00:00:00Z",
    )
    assert out["enrolled"] is False
    assert out["reason"] == "route_disabled"
    assert bsr._read() == []  # nothing written


def test_enroll_skips_non_signal(armed):
    out = bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=_lead(intent_score=10),
        now_iso="2026-06-30T00:00:00Z",
    )
    assert out == {"enrolled": False, "reason": "not_a_buying_signal"}
    assert bsr._read() == []


def test_enroll_writes_record_and_is_idempotent(armed):
    lead = _lead(recommended_action="book_call", intent_score=80,
                 contact_offered="owner@acme.com")
    first = bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=lead, now_iso="2026-06-30T00:00:00Z",
        email="owner@acme.com", company="Acme Plumbing", call_id="c1",
    )
    assert first["enrolled"] is True
    recs = bsr._read()
    assert len(recs) == 1
    assert recs[0]["prospect_id"] == "p1"
    assert recs[0]["sequence_id"] == "buying_signal"
    assert recs[0]["email"] == "owner@acme.com"
    assert recs[0]["status"] == "active"

    # Second call for the same active prospect does not duplicate.
    second = bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=lead, now_iso="2026-06-30T01:00:00Z",
    )
    assert second == {"enrolled": False, "reason": "already_enrolled"}
    assert len(bsr._read()) == 1


# ---------------------------------------------------------------------------
# Dispatch (dormant + armed)
# ---------------------------------------------------------------------------

def test_dispatch_dry_run_plans_without_sending(armed, monkeypatch):
    sent: list = []
    monkeypatch.setattr(outreach_service, "send_message",
                        lambda req: sent.append(req) or {"message_id": "x"})
    bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=_lead(recommended_action="book_call"),
        now_iso="2026-06-30T00:00:00Z", email="owner@acme.com",
    )
    res = bsr.dispatch_due_buying_signal(now_iso="2026-06-30T00:00:00Z", dry_run=True)
    assert any(r["action"] == "send" and r.get("planned") for r in res)
    assert sent == []  # nothing actually sent
    # enrollment untouched (no completed steps)
    assert bsr._read()[0]["completed_steps"] == []


def _composer(message, rec):
    # Stand-in for the operator's real per-prospect composition.
    return ("Great talking, " + (rec.get("company") or "there"), "Real send-ready body.")


def test_dispatch_armed_sends_and_marks(armed, monkeypatch):
    sent: list = []
    monkeypatch.setattr(outreach_service, "send_message",
                        lambda req: sent.append(req) or {"message_id": "x", "ts": "t"})
    bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=_lead(recommended_action="book_call"),
        now_iso="2026-06-30T00:00:00Z", email="owner@acme.com",
    )
    res = bsr.dispatch_due_buying_signal(
        now_iso="2026-06-30T00:00:00Z", dry_run=False, composer=_composer)
    sends = [r for r in res if r.get("sent")]
    assert len(sends) == 1
    assert len(sent) == 1
    assert sent[0].to == "owner@acme.com"
    assert sent[0].channel == "email"
    assert sent[0].subject  # non-empty (email validator requires it)
    assert sent[0].body == "Real send-ready body."
    # Touch 1 (day 0) is now marked sent on the enrollment.
    assert bsr._read()[0]["completed_steps"] == [1]


def test_dispatch_armed_without_composer_fails_closed(armed, monkeypatch):
    """No composer -> never send (would otherwise leak the internal brief)."""
    sent: list = []
    monkeypatch.setattr(outreach_service, "send_message",
                        lambda req: sent.append(req) or {"message_id": "x"})
    bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=_lead(recommended_action="book_call"),
        now_iso="2026-06-30T00:00:00Z", email="owner@acme.com",
    )
    res = bsr.dispatch_due_buying_signal(now_iso="2026-06-30T00:00:00Z", dry_run=False)
    assert any(r.get("reason") == "no_composer" for r in res)
    assert sent == []
    assert bsr._read()[0]["completed_steps"] == []  # not marked sent


def test_flag_binds_from_env_via_bootstrap(monkeypatch):
    """The real env->Settings path (bootstrap_settings), NOT a monkeypatched
    get_settings — guards the wiring that arms the flag in the container."""
    from backend.common.settings import reload_settings
    from backend.common.config import get_settings

    monkeypatch.setenv("OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED", "true")
    monkeypatch.setenv("OUTREACH_BUYING_SIGNAL_INTENT_THRESHOLD", "55")
    reload_settings()
    s = get_settings()
    assert s.outreach_buying_signal_route_enabled is True
    assert s.outreach_buying_signal_intent_threshold == 55

    monkeypatch.delenv("OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED", raising=False)
    reload_settings()
    assert get_settings().outreach_buying_signal_route_enabled is False


def test_dispatch_armed_skips_when_no_email(armed, monkeypatch):
    sent: list = []
    monkeypatch.setattr(outreach_service, "send_message",
                        lambda req: sent.append(req) or {"message_id": "x"})
    bsr.maybe_enroll_buying_signal(
        prospect_id="p1", lead=_lead(recommended_action="book_call"),
        now_iso="2026-06-30T00:00:00Z", email="",  # no contact captured
    )
    res = bsr.dispatch_due_buying_signal(
        now_iso="2026-06-30T00:00:00Z", dry_run=False, composer=_composer)
    assert any(r.get("reason") == "no_email" for r in res)
    assert sent == []
