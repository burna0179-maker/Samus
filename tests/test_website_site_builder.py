"""Headless site generator — taste-governed static frontend."""

from __future__ import annotations

from backend.website.models import WebsiteBrief, WebsitePage
from backend.website.site_builder import GeneratedSite, build_static_site


def _brief(**over):
    base = dict(
        business_name="Sample Cleaning",
        business_description="Family-owned professional house cleaning serving <city>, <state>.",
        industry="House Cleaning, Residential Cleaning, Airbnb Turnover",
        contact_email="hello@mighty.test",
        contact_phone="<phone>",
        address="<street>, <city>, <state> 97624",
        brand_colors=["#0E7C8B", "#5CB544", "#F2EC4F"],
        pages=[
            WebsitePage(
                slug="home",
                title="Home",
                content={
                    "headline": "If you want a MIGHTY clean, give us a call!",
                    "intro": "Family owned and operated, over 10 years making homes sparkle.",
                },
            ),
            WebsitePage(
                slug="about",
                title="About",
                content={"body": "We are a family-owned cleaning service."},
            ),
            WebsitePage(
                slug="services",
                title="Services",
                content={
                    "body": "Regular and one-time deep cleaning for homes and businesses.",
                    "list": "Deep Cleaning | Airbnb Turnovers | Commercial | Residential",
                },
            ),
            WebsitePage(
                slug="contact", title="Contact", content={"body": "Call today for a free quote."}
            ),
        ],
    )
    base.update(over)
    return WebsiteBrief(**base)


def test_generates_index_html():
    site = build_static_site(_brief())
    assert isinstance(site, GeneratedSite)
    assert "index.html" in site.files
    assert "index.html" in site.pages


def test_generates_legal_subpages_with_footer_links():
    site = build_static_site(_brief())
    assert "privacy.html" in site.files and "terms.html" in site.files
    assert "Privacy Policy" in site.files["privacy.html"]
    assert "Governing Law" in site.files["terms.html"]
    # footer of the index links to the clean URLs CF Pages serves
    assert '"/privacy"' in site.files["index.html"] and '"/terms"' in site.files["index.html"]
    # compliance report attached for the operator
    assert site.compliance and site.compliance["jurisdiction"]["country"] == "US"


def test_terms_governing_law_uses_client_state():
    # Sample Cleaning address is in OR -> governing law = State of OR
    site = build_static_site(_brief())
    assert "State of OR" in site.files["terms.html"]


def test_legal_pages_can_be_disabled():
    from types import SimpleNamespace

    site = build_static_site(
        _brief(),
        settings=SimpleNamespace(
            website_legal_pages_enabled=False, website_design_intelligence_enabled=True
        ),
    )
    assert "privacy.html" not in site.files and "terms.html" not in site.files


def test_html_passes_taste_audit():
    site = build_static_site(_brief())
    assert site.taste_audit["passed"] is True
    assert site.taste_audit["grade"] in ("A", "B")


def test_html_contains_copy_and_brand_and_jsonld():
    site = build_static_site(_brief())
    html = site.files["index.html"]
    assert "MIGHTY clean" in html
    assert "#0E7C8B" in html  # brand accent in CSS vars
    assert "application/ld+json" in html  # JSON-LD in head
    assert "Sample Cleaning" in html
    assert "<phone>" in html
    assert "Deep Cleaning" in html  # services list rendered


def test_no_em_dash_in_output():
    # business desc with an em-dash should be sanitized out of the final HTML.
    b = _brief(business_description="Cleaning — fast — reliable for homes and rentals.")
    site = build_static_site(b)
    assert "—" not in site.files["index.html"] and "–" not in site.files["index.html"]


def test_design_intelligence_applies_industry_fonts():
    site = build_static_site(_brief())
    assert site.design_system is not None
    di_heading = (site.design_system.get("typography") or {}).get("heading")
    assert di_heading and di_heading in site.files["index.html"]
    assert di_heading != "Inter"  # never the discouraged default as heading


def test_brand_colors_win_over_di_palette():
    assert "#0E7C8B" in build_static_site(_brief()).files["index.html"]


def test_di_palette_used_when_no_brand_colors():
    site = build_static_site(_brief(brand_colors=[]))
    primary = (site.design_system or {}).get("palette", {}).get("primary")
    assert primary and primary in site.files["index.html"]


def test_has_glassmorphism_gradients_and_motion():
    html = build_static_site(_brief()).files["index.html"]
    assert "backdrop-filter:blur" in html  # glassmorphism
    assert "linear-gradient" in html and "radial-gradient" in html  # gradients
    assert "meshmove" in html  # animated gradient mesh
    assert "glass" in html  # glass utility applied


def test_can_disable_design_intelligence():
    from types import SimpleNamespace

    html = build_static_site(
        _brief(), settings=SimpleNamespace(website_design_intelligence_enabled=False)
    ).files["index.html"]
    assert "Outfit" in html  # falls back to the house font


def test_reveal_safe_without_js():
    html = build_static_site(_brief()).files["index.html"]
    # content hidden only when the .js class is present (set by inline script),
    # so a no-JS browser still shows everything.
    assert ".js .reveal" in html
    assert "classList.add('js')" in html


def test_no_h_screen_uses_dvh():
    html = build_static_site(_brief()).files["index.html"]
    assert "min-h-[100dvh]" in html
    assert "h-screen" not in html


def test_no_scroll_listener_uses_intersection_observer():
    html = build_static_site(_brief()).files["index.html"]
    assert "IntersectionObserver" in html
    assert "addEventListener('scroll'" not in html and 'addEventListener("scroll"' not in html


def test_hero_uses_supplied_image():
    site = build_static_site(
        _brief(), media={"hero_image": "https://static.wixstatic.com/media/hero.png"}
    )
    html = site.files["index.html"]
    assert "https://static.wixstatic.com/media/hero.png" in html
    assert not site.warnings  # hero media supplied -> no gradient warning


def test_hero_uses_veo_video_when_supplied():
    site = build_static_site(_brief(), media={"hero_video": "https://v/clip.mp4"})
    html = site.files["index.html"]
    assert "<video" in html and "https://v/clip.mp4" in html


def test_warns_when_no_hero_media():
    site = build_static_site(_brief())
    assert any("hero media" in w for w in site.warnings)


def test_to_dict_omits_file_contents():
    d = build_static_site(_brief()).to_dict()
    assert isinstance(d["files"]["index.html"], int)  # size, not content
    assert d["taste_audit"]["passed"] is True


def test_quote_modal_present_with_location_fields():
    html = build_static_site(_brief()).files["index.html"]
    assert 'id="quoteModal"' in html
    for fld in (
        'name="name"',
        'name="business"',
        'name="email"',
        'name="phone"',
        'name="city"',
        'name="state"',
        'name="country"',
        'name="message"',
    ):
        assert fld in html, fld
    assert "data-quote" in html  # CTA opens the modal


def test_quote_form_posts_to_intake_endpoint():
    html = build_static_site(_brief(intake_endpoint="https://api.hf.test/intake/onboarding")).files[
        "index.html"
    ]
    assert 'data-endpoint="https://api.hf.test/intake/onboarding"' in html


def test_schedule_now_button_when_calendar_set():
    html = build_static_site(_brief(scheduling_url="https://calendar.app.google/abc")).files[
        "index.html"
    ]
    assert "Schedule Now" in html and "https://calendar.app.google/abc" in html


def test_no_schedule_button_without_calendar():
    assert "Schedule Now" not in build_static_site(_brief(scheduling_url="")).files["index.html"]


def test_card_icons_present_as_svg():
    html = build_static_site(_brief()).files["index.html"]
    assert "<svg" in html  # cleaning line-icons (hero strip + service cards)


def test_3d_animated_name_present():
    html = build_static_site(_brief()).files["index.html"]
    assert "name3d" in html and "float3d" in html
    assert "Sample Cleaning" in html


def test_phone_click_to_call_in_header_and_hero():
    html = build_static_site(_brief()).files["index.html"]
    header = html.split("</header>")[0]
    assert 'href="tel:5308404104"' in header  # header click-to-call
    assert "Call <phone>" in html  # hero/contact CTA button


def test_no_tel_link_without_phone():
    html = build_static_site(_brief(contact_phone="")).files["index.html"]
    assert 'href="tel:' not in html


def test_header_nav_reaches_services_about_contact():
    html = build_static_site(_brief()).files["index.html"]
    header = html.split("</header>")[0]
    for label in ("Services", "About", "Contact"):
        assert label in header, label


def test_promo_band_renders_only_when_supplied():
    plain = build_static_site(_brief()).files["index.html"]
    assert 'id="promo"' not in plain  # inert without opt-in
    pages = _brief().pages
    pages[0].content["promo"] = "Free estimates - call today"
    promo = build_static_site(_brief(pages=pages)).files["index.html"]
    assert 'id="promo"' in promo and "Free estimates - call today" in promo


def test_placeholder_sections_render_only_when_supplied():
    plain = build_static_site(_brief()).files["index.html"]
    assert "Demo preview" not in plain
    pages = _brief().pages
    pages[0].content.update(
        {
            "trust_line": "Rated 5★ by 8 customers on Google",
            "testimonials_placeholder": "Your customer reviews featured here",
            "credentials_placeholder": "Your license & certifications displayed here",
            "portfolio_placeholder": "Photos of your completed projects showcased here",
        }
    )
    html = build_static_site(_brief(pages=pages)).files["index.html"]
    assert "Rated 5★ by 8 customers on Google" in html
    assert html.count("Demo preview") == 3
    assert 'id="reviews"' in html and 'id="portfolio"' in html


def test_taste_audit_still_passes_with_demo_sections():
    pages = _brief().pages
    pages[0].content.update(
        {
            "promo": "Free estimates - call today",
            "trust_line": "Rated 5★ by 8 customers on Google",
            "testimonials_placeholder": "Your customer reviews featured here",
            "credentials_placeholder": "Your license & certifications displayed here",
            "portfolio_placeholder": "Photos of your completed projects showcased here",
        }
    )
    site = build_static_site(_brief(pages=pages))
    assert site.taste_audit["passed"] is True
