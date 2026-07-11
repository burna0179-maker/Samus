"""verified_top_finding: Morgan must never open with 'you have no website' to a
business that actually has one (crawler false-positive). Places is mocked."""
from __future__ import annotations

import backend.prospecting.callsheet as cs
import backend.website.presence_check as pc
from backend.prospecting.models import ProspectRecord
from backend.website.presence_check import PresenceVerdict


def _rec(**kw):
    base = dict(company_name="Acme Auto", city="Yuba City", state="CA",
                website_status="no_website", website_url="")
    base.update(kw)
    return ProspectRecord(**base)


def test_off_by_default_is_pure_top_finding(monkeypatch):
    monkeypatch.setattr(cs, "_presence_verify_enabled", lambda: False)
    p = _rec()
    assert cs.verified_top_finding(p) == cs._top_finding(p)
    assert "no real website" in cs.verified_top_finding(p)


def test_has_live_site_avoids_false_pitch(monkeypatch):
    monkeypatch.setattr(cs, "_presence_verify_enabled", lambda: True)
    monkeypatch.setattr(
        pc, "verify_presence",
        lambda *a, **k: PresenceVerdict(False, "Places lists a site",
                                        website="http://acmeauto.com"))
    p = _rec(review_rating="3.5", review_count="10")
    finding = cs.verified_top_finding(p)
    assert "no real website" not in finding          # the false pitch is gone
    assert p.website_status == "access_blocked"       # misclassification corrected
    assert p.website_url == "http://acmeauto.com"


def test_genuinely_no_site_keeps_finding(monkeypatch):
    monkeypatch.setattr(cs, "_presence_verify_enabled", lambda: True)
    monkeypatch.setattr(
        pc, "verify_presence",
        lambda *a, **k: PresenceVerdict(True, "no website — buildable"))
    p = _rec()
    assert "no real website" in cs.verified_top_finding(p)


def test_non_website_finding_not_reverified(monkeypatch):
    # a security/seo finding must not trigger a Places call at all
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return PresenceVerdict(True, "x")

    monkeypatch.setattr(cs, "_presence_verify_enabled", lambda: True)
    monkeypatch.setattr(pc, "verify_presence", _spy)
    p = _rec(website_status="live", security_grade="F")
    finding = cs.verified_top_finding(p)
    assert "security warning" in finding
    assert called["n"] == 0


def test_verify_error_keeps_finding(monkeypatch):
    monkeypatch.setattr(cs, "_presence_verify_enabled", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("places down")

    monkeypatch.setattr(pc, "verify_presence", _boom)
    p = _rec()
    assert "no real website" in cs.verified_top_finding(p)   # fail-soft
