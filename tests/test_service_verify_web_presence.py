"""Root fix: verify_web_presence corrects a no_website false-positive at the
source (before callsheet/demo/Morgan ever see it). Places is mocked."""

from __future__ import annotations

import backend.prospecting.service as svc
import backend.website.presence_check as pc
from backend.prospecting.models import ProspectRecord
from backend.website.presence_check import PresenceVerdict


def _rec(**kw):
    base = dict(
        company_name="Acme Auto",
        city="Yuba City",
        state="CA",
        website_status="no_website",
        website_url="",
    )
    base.update(kw)
    return ProspectRecord(**base)


def test_corrects_no_website_when_places_has_site(monkeypatch):
    monkeypatch.setattr(
        pc,
        "verify_presence",
        lambda *a, **k: PresenceVerdict(
            False, "Places lists a site", website="http://acmeauto.com"
        ),
    )
    out = svc.verify_web_presence([_rec()])
    assert out[0].website_url == "http://acmeauto.com"
    assert out[0].website_status == "access_blocked"  # no longer a "no site" hook


def test_leaves_genuine_no_website(monkeypatch):
    monkeypatch.setattr(
        pc, "verify_presence", lambda *a, **k: PresenceVerdict(True, "no website — buildable")
    )
    out = svc.verify_web_presence([_rec()])
    assert out[0].website_status == "no_website"


def test_skips_prospects_that_already_have_a_url(monkeypatch):
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return PresenceVerdict(True, "x")

    monkeypatch.setattr(pc, "verify_presence", _spy)
    out = svc.verify_web_presence([_rec(website_url="http://x.com", website_status="live")])
    assert called["n"] == 0  # not re-verified


def test_fail_soft_leaves_prospect_unchanged(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("places down")

    monkeypatch.setattr(pc, "verify_presence", _boom)
    out = svc.verify_web_presence([_rec()])
    assert out[0].website_status == "no_website"
