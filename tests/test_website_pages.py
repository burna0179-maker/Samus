"""Marketing sub-pages — the multi-page depth that lifts the build toward an
agency-grade deliverable (Services / Gallery / FAQ / Reviews / service areas).

Honesty invariants are asserted here on purpose: no fabricated testimonials,
no invented coverage areas, FAQ carries rich-result JSON-LD.
"""

from __future__ import annotations

from backend.website.models import WebsiteBrief, WebsitePage
from backend.website.pages import _google_form_embed, build_marketing_pages
from backend.website.site_builder import build_static_site


def _brief(**over):
    base = dict(
        business_name="Sample Cleaning",
        business_description="Family-owned professional house cleaning serving <city>, <state>.",
        industry="House Cleaning, Residential Cleaning, Airbnb Turnover",
        contact_email="support@example.com",
        contact_phone="<phone>",
        address="<street>, <city>, <state> 97624",
        brand_colors=["#0E7C8B", "#5CB544", "#F2EC4F"],
        pages=[
            WebsitePage(
                slug="services",
                title="Services",
                content={"list": "Deep Cleaning | Airbnb Turnovers | Commercial | Residential"},
            ),
        ],
    )
    base.update(over)
    return WebsiteBrief(**base)


def test_build_marketing_pages_core_set():
    pages = build_marketing_pages(_brief(), media={})
    files = {p["file"] for p in pages}
    assert {"services.html", "gallery.html", "faq.html", "reviews.html"} <= files


def test_faq_page_has_faqpage_jsonld():
    pages = {p["file"]: p for p in build_marketing_pages(_brief(), media={})}
    assert '"FAQPage"' in pages["faq.html"]["head"]


def test_services_page_lists_each_service():
    pages = {p["file"]: p for p in build_marketing_pages(_brief(), media={})}
    html = pages["services.html"]["content"]
    for svc in ("Deep Cleaning", "Airbnb Turnovers", "Commercial", "Residential"):
        assert svc in html


def test_move_out_service_gets_dedicated_blurb():
    brief = _brief(
        pages=[
            WebsitePage(
                slug="services",
                title="Services",
                content={"list": "Deep Cleaning | Move-Out Cleaning | Airbnb Turnovers"},
            )
        ]
    )
    html = {p["file"]: p for p in build_marketing_pages(brief, media={})}["services.html"][
        "content"
    ]
    assert "Move-Out Cleaning" in html
    # not the generic fallback — the move-specific copy
    assert "Moving in or out" in html


def test_reviews_page_has_no_fabricated_testimonials():
    pages = {p["file"]: p for p in build_marketing_pages(_brief(), media={})}
    html = pages["reviews.html"]["content"].lower()
    # honest scaffold, not invented 5-star quotes
    assert "5 stars" not in html and "â˜…" not in html
    # with no review_url it falls back to a work-with-us CTA
    assert "data-quote" in pages["reviews.html"]["content"]


def test_reviews_page_uses_review_url_when_supplied():
    pages = {
        p["file"]: p
        for p in build_marketing_pages(_brief(review_url="https://g.page/r/mhh/review"), media={})
    }
    assert "https://g.page/r/mhh/review" in pages["reviews.html"]["content"]
    assert "Leave a Review" in pages["reviews.html"]["content"]


def test_area_page_defaults_to_business_city_only():
    pages = build_marketing_pages(_brief(), media={})
    areas = [p for p in pages if p.get("area")]
    assert len(areas) == 1
    assert areas[0]["file"] == "areas/chiloquin.html"
    assert "<city>" in areas[0]["content"]
    assert '"LocalBusiness"' in areas[0]["head"]


def test_area_pages_never_invent_coverage():
    # explicit service_areas -> exactly those, nothing fabricated
    pages = build_marketing_pages(
        _brief(service_areas=["Klamath Falls", "Sprague River"]), media={}
    )
    area_files = {p["file"] for p in pages if p.get("area")}
    assert area_files == {"areas/klamath-falls.html", "areas/sprague-river.html"}


# --- integration through build_static_site ---------------------------------


def test_multipage_wired_into_site_build():
    site = build_static_site(_brief())
    for f in ("services.html", "gallery.html", "faq.html", "reviews.html", "areas/chiloquin.html"):
        assert f in site.files and f in site.pages
    # homepage nav links to the marketing pages (clean URLs)
    idx = site.files["index.html"]
    for href in ('"/services"', '"/gallery"', '"/faq"', '"/reviews"'):
        assert href in idx
    # sub-pages carry the quote modal so the nav Quote button works everywhere
    assert "quoteModal" in site.files["services.html"]
    # every generated page passes the taste gate
    assert site.taste_audit["passed"]


def _careers(brief):
    pages = {p["file"]: p for p in build_marketing_pages(brief, media={})}
    return pages["careers.html"]["content"]


def test_careers_page_in_core_set():
    files = {p["file"] for p in build_marketing_pages(_brief(), media={})}
    assert "careers.html" in files


def test_careers_embeds_google_form_when_supplied():
    html = _careers(_brief(careers_form_url="https://docs.google.com/forms/d/e/1FAIpQLSx/viewform"))
    assert "<iframe" in html
    assert "docs.google.com/forms/d/e/1FAIpQLSx/viewform?embedded=true" in html
    assert "Apply online" in html
    assert "Open it in a new tab" in html


def test_careers_accepts_forms_gle_short_link():
    html = _careers(_brief(careers_form_url="https://forms.gle/AbC123"))
    assert "<iframe" in html and "https://forms.gle/AbC123" in html


def test_careers_rejects_non_google_form_and_falls_back_to_mailto():
    # an attacker-supplied brief must not get its arbitrary origin framed
    html = _careers(_brief(careers_form_url="https://evil.example/forms/d/e/x/viewform"))
    assert "<iframe" not in html
    assert "mailto:support@example.com" in html


def test_careers_mailto_fallback_without_form():
    html = _careers(_brief())
    assert "<iframe" not in html
    assert "mailto:support@example.com" in html


def test_google_form_embed_helper_rejects_unsafe():
    assert _google_form_embed("javascript:alert(1)") == ""
    assert _google_form_embed("https://docs.google.com/spreadsheets/d/x/edit") == ""
    assert _google_form_embed("") == ""


def test_csp_allows_google_forms_frame():
    from backend.website.deploy_cloudflare import build_security_headers

    headers = build_security_headers()
    assert "frame-src https://docs.google.com https://forms.gle" in headers


def test_scheduling_button_rendered_when_url_supplied():
    site = build_static_site(_brief(scheduling_url="https://calendar.app.google/abc123"))
    idx = site.files["index.html"]
    assert "Schedule Now" in idx and "https://calendar.app.google/abc123" in idx


def test_no_scheduling_button_without_url():
    site = build_static_site(_brief())
    assert "Schedule Now" not in site.files["index.html"]


def test_multipage_disabled_keeps_legal_only():
    class _S:
        website_multipage_enabled = False
        website_legal_pages_enabled = True
        website_design_intelligence_enabled = True

    site = build_static_site(_brief(), settings=_S())
    assert "services.html" not in site.files
    assert "privacy.html" in site.files  # legal still generated
    # nav degrades to just the contact link
    assert '"/services"' not in site.files["index.html"]
