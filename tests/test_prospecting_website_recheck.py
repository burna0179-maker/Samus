"""Website-status verification re-poll — service.recheck_unreachable.

A transient crawl failure at discovery time mislabels a healthy site as
unreachable for the day and inflates its lead score off an unmeasured SEO; the
post-run re-poll (Step 3.5) re-classifies + re-scores anything that responds.
"""

from __future__ import annotations

from backend.prospecting import service
from backend.prospecting.models import ProspectRecord


def _p(**kw):
    base = dict(
        prospect_id="pr_x",
        company_name="Acme HVAC",
        industry="dentist",
        website_url="https://acme.example/",
    )
    base.update(kw)
    return ProspectRecord(**base)


def _live_page(url):
    return {"status_code": 200, "html": "<h1>hi</h1>", "final_url": url, "fetch_error": None}


def test_recheck_recovers_a_transiently_unreachable_site(monkeypatch):
    from backend.prospecting import crawler, seo_audit

    monkeypatch.setattr(crawler, "fetch_homepage", lambda url: _live_page(url))
    monkeypatch.setattr(crawler, "classify_website", lambda page: "live")
    monkeypatch.setattr(seo_audit, "score_seo", lambda page: (55, []))

    p = _p(website_status="unreachable_timeout", seo_score=0, lead_score=84, call_priority="hot")
    out = service.recheck_unreachable([p])
    assert out[0].website_status == "live"
    assert out[0].seo_score == 55
    # A live site with a measured SEO score no longer collects the inflated
    # unmeasured-SEO credit, so the lead score falls.
    assert out[0].lead_score < 84


def test_recheck_confirms_a_still_failing_site(monkeypatch):
    from backend.prospecting import crawler

    monkeypatch.setattr(
        crawler,
        "fetch_homepage",
        lambda url: {
            "status_code": 0,
            "html": None,
            "final_url": url,
            "fetch_error": "timeout: read timed out",
        },
    )
    monkeypatch.setattr(crawler, "classify_website", lambda page: "unreachable_timeout")

    p = _p(website_status="unreachable_timeout", seo_score=0, lead_score=84)
    out = service.recheck_unreachable([p])
    assert out[0].website_status == "unreachable_timeout"
    assert out[0].lead_score == 84  # unchanged — confirmed non-response


def test_recheck_skips_live_and_genuine_no_website(monkeypatch):
    from backend.prospecting import crawler

    calls: list[str] = []
    monkeypatch.setattr(crawler, "fetch_homepage", lambda url: calls.append(url) or _live_page(url))

    live = _p(prospect_id="pr_live", website_status="live")
    absent = _p(prospect_id="pr_none", website_status="no_website", website_url="")
    parked = _p(prospect_id="pr_park", website_status="parked")
    out = service.recheck_unreachable([live, absent, parked])
    assert calls == []  # none of these statuses is re-polled
    assert [x.website_status for x in out] == ["live", "no_website", "parked"]


def test_recheck_reclassifies_to_a_different_failure(monkeypatch):
    """A timeout that re-polls to an access_blocked 403 is re-categorised —
    still not live, but now the honest status."""
    from backend.prospecting import crawler

    monkeypatch.setattr(
        crawler,
        "fetch_homepage",
        lambda url: {"status_code": 403, "html": None, "final_url": url, "fetch_error": None},
    )
    monkeypatch.setattr(crawler, "classify_website", lambda page: "access_blocked")

    p = _p(website_status="unreachable_timeout", seo_score=0, lead_score=80)
    out = service.recheck_unreachable([p])
    assert out[0].website_status == "access_blocked"


def test_recheck_is_best_effort_on_fetch_error(monkeypatch):
    """A fault inside the re-poll leaves the prospect exactly as the run left it."""
    from backend.prospecting import crawler

    def _boom(url):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(crawler, "fetch_homepage", _boom)
    p = _p(website_status="unreachable_timeout", seo_score=0, lead_score=84)
    out = service.recheck_unreachable([p])
    assert out[0].website_status == "unreachable_timeout"
    assert out[0].lead_score == 84
