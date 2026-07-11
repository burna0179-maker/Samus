"""Tests for backend.prospecting.enrichment."""
from __future__ import annotations

import pytest

from backend.prospecting.enrichment import (
    _facebook_handle,
    _is_facebook_login_wall,
    enrich_from_page_with_fallback,
    extract_facebook_signals,
    extract_owner_signals,
    facebook_about_url,
    merge_signals,
)


# ---------------------------------------------------------------------------
# extract_owner_signals — empty / no-html paths
# ---------------------------------------------------------------------------

def test_extract_owner_signals_empty_html_returns_all_empty():
    sig = extract_owner_signals("")
    expected_keys = {
        "owner_name", "owner_email", "owner_title", "owner_linkedin_url",
        "contact_emails", "social_facebook", "social_instagram", "social_linkedin",
        "business_description",
    }
    assert set(sig.keys()) == expected_keys
    assert all(v == "" for v in sig.values())


def test_extract_owner_signals_none_html_returns_all_empty():
    sig = extract_owner_signals(None)
    assert all(v == "" for v in sig.values())


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------

def test_mailto_link_picked_up():
    html = '<a href="mailto:john@navaccounts.com">Email John</a>'
    sig = extract_owner_signals(html)
    assert sig["owner_email"] == "john@navaccounts.com"
    assert "john@navaccounts.com" in sig["contact_emails"]


def test_personal_email_preferred_over_generic():
    """info@ should lose to a personal-looking address."""
    html = """
      <a href="mailto:info@biz.com">general</a>
      <a href="mailto:sarah@biz.com">owner</a>
    """
    sig = extract_owner_signals(html)
    assert sig["owner_email"] == "sarah@biz.com"
    # contact_emails should include both, info first (mailto order)
    assert "info@biz.com" in sig["contact_emails"]
    assert "sarah@biz.com" in sig["contact_emails"]


def test_junk_emails_filtered():
    html = """
      <a href="mailto:noreply@biz.com">do not reply</a>
      <a href="mailto:postmaster@biz.com">postmaster</a>
      <a href="mailto:owner@biz.com">owner</a>
      <a href="mailto:webmaster@biz.com">webmaster</a>
    """
    sig = extract_owner_signals(html)
    assert sig["owner_email"] == "owner@biz.com"
    # Junk addresses must not appear in contact_emails either
    for junk in ("noreply", "postmaster", "webmaster"):
        assert junk not in sig["contact_emails"]


def test_plain_text_emails_extracted():
    html = "Reach us at hello@yctax.solutions or call (530) 402-8274"
    sig = extract_owner_signals(html)
    # hello@ is generic so owner_email falls through to first found
    # (mailto: list empty, plain-text adds hello@). With no personal-looking
    # email, owner_email == "hello@..."
    assert sig["owner_email"] == "hello@yctax.solutions"
    assert "hello@yctax.solutions" in sig["contact_emails"]


def test_contact_emails_capped_at_five():
    emails = [f"<a href='mailto:user{i}@biz.com'>x</a>" for i in range(10)]
    html = " ".join(emails)
    sig = extract_owner_signals(html)
    parts = sig["contact_emails"].split("; ")
    assert len(parts) == 5


def test_emails_deduped():
    html = """
      mailto:john@biz.com
      <a href="mailto:john@biz.com">again</a>
      plain text john@biz.com
    """
    sig = extract_owner_signals(html)
    parts = sig["contact_emails"].split("; ")
    assert parts.count("john@biz.com") == 1


# ---------------------------------------------------------------------------
# Social media extraction
# ---------------------------------------------------------------------------

def test_facebook_profile_extracted():
    html = '<a href="https://www.facebook.com/SutterButtesRealEstate">FB</a>'
    sig = extract_owner_signals(html)
    assert sig["social_facebook"] == "https://www.facebook.com/SutterButtesRealEstate"


def test_facebook_sharer_url_skipped():
    """sharer.php / share dialogs are not the prospect's profile."""
    html = """
      <a href="https://www.facebook.com/sharer/sharer.php?u=...">share</a>
      <a href="https://www.facebook.com/RealBusinessProfile">our page</a>
    """
    sig = extract_owner_signals(html)
    assert "RealBusinessProfile" in sig["social_facebook"]
    assert "sharer" not in sig["social_facebook"]


def test_facebook_oauth_dialog_url_skipped():
    """FB Login OAuth callback URLs (v2.10/dialog/oauth) are not profiles.

    Regression: Century 21's site exposed a Facebook Login button whose
    OAuth callback URL was matching the FB regex before the negative
    lookahead caught FB API version paths like ``v2.10``.
    """
    html = """
      <a href="https://www.facebook.com/v2.10/dialog/oauth?client_id=...">FB Login</a>
      <a href="https://www.facebook.com/Century21SelectGroup">our page</a>
    """
    sig = extract_owner_signals(html)
    assert "Century21SelectGroup" in sig["social_facebook"]
    assert "dialog" not in sig["social_facebook"].lower()
    assert "oauth" not in sig["social_facebook"].lower()


def test_instagram_profile_extracted():
    html = '<a href="https://instagram.com/biz_handle">IG</a>'
    sig = extract_owner_signals(html)
    assert sig["social_instagram"] == "https://instagram.com/biz_handle"


def test_instagram_post_path_skipped():
    """instagram.com/p/<id> is a single-post URL, not a profile."""
    html = """
      <a href="https://www.instagram.com/p/CXyz/">single post</a>
      <a href="https://www.instagram.com/yc_dentistry/">profile</a>
    """
    sig = extract_owner_signals(html)
    assert "/p/" not in sig["social_instagram"]
    assert "yc_dentistry" in sig["social_instagram"]


def test_linkedin_personal_profile_populates_owner_linkedin():
    html = '<a href="https://www.linkedin.com/in/john-doe-93b">LI</a>'
    sig = extract_owner_signals(html)
    assert sig["social_linkedin"] == "https://www.linkedin.com/in/john-doe-93b"
    assert sig["owner_linkedin_url"] == "https://www.linkedin.com/in/john-doe-93b"


def test_linkedin_company_page_does_not_populate_owner_linkedin():
    """linkedin.com/company/<name> is a company page, not an owner profile."""
    html = '<a href="https://www.linkedin.com/company/nav-accounts">LI</a>'
    sig = extract_owner_signals(html)
    assert sig["social_linkedin"] == "https://www.linkedin.com/company/nav-accounts"
    assert sig["owner_linkedin_url"] == ""


# ---------------------------------------------------------------------------
# Owner name extraction (high-signal only)
# ---------------------------------------------------------------------------

def test_owner_name_from_jsonld_person():
    html = """
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Person",
       "name":"Sarah Henley","jobTitle":"Broker"}
      </script>
    """
    sig = extract_owner_signals(html)
    assert sig["owner_name"] == "Sarah Henley"


def test_owner_name_from_author_meta():
    html = '<meta name="author" content="John Counihan">'
    sig = extract_owner_signals(html)
    assert sig["owner_name"] == "John Counihan"


def test_owner_name_rejects_business_shape():
    """JSON-LD Person sometimes carries the business name. Reject those."""
    html = """
      <script type="application/ld+json">
      {"@type":"Person","name":"Sutter Buttes Real Estate Group LLC"}
      </script>
    """
    sig = extract_owner_signals(html)
    assert sig["owner_name"] == ""


def test_owner_name_none_when_neither_signal_present():
    html = "<html><body><h1>Welcome</h1></body></html>"
    sig = extract_owner_signals(html)
    assert sig["owner_name"] == ""


# ---------------------------------------------------------------------------
# Business description extraction
# ---------------------------------------------------------------------------

def test_business_description_from_og_description():
    html = '<meta property="og:description" content="Family-run HVAC repair serving Yuba City since 1998.">'
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "Family-run HVAC repair serving Yuba City since 1998."


def test_business_description_from_meta_description():
    html = '<meta name="description" content="Trusted local dentist accepting new patients.">'
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "Trusted local dentist accepting new patients."


def test_business_description_handles_reversed_attribute_order():
    """content=... before name=... — the alt regex must still match."""
    html = '<meta content="Roofing contractor, free estimates." name="description">'
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "Roofing contractor, free estimates."


def test_business_description_prefers_og_over_meta():
    html = """
      <meta name="description" content="meta version of the blurb">
      <meta property="og:description" content="og version of the blurb">
    """
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "og version of the blurb"


def test_business_description_from_jsonld_when_no_meta():
    html = """
      <script type="application/ld+json">
      {"@type":"LocalBusiness","name":"Acme",
       "description":"Full-service accounting firm for small businesses."}
      </script>
    """
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "Full-service accounting firm for small businesses."


def test_business_description_collapses_whitespace():
    html = '<meta name="description" content="Local   plumber.\n  Fast\tservice.">'
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "Local plumber. Fast service."


def test_business_description_truncated_to_single_line():
    long_blurb = "We do everything. " * 30  # ~540 chars
    html = f'<meta name="description" content="{long_blurb.strip()}">'
    sig = extract_owner_signals(html)
    assert len(sig["business_description"]) <= 243  # 240 + "..."
    assert sig["business_description"].endswith("...")


def test_business_description_empty_when_page_carries_none():
    html = "<html><body><h1>Welcome</h1><p>Some text.</p></body></html>"
    sig = extract_owner_signals(html)
    assert sig["business_description"] == ""


def test_business_description_unescapes_entities():
    html = '<meta name="description" content="Joe&#39;s Diner &amp; Catering">'
    sig = extract_owner_signals(html)
    assert sig["business_description"] == "Joe's Diner & Catering"


# ---------------------------------------------------------------------------
# merge_signals
# ---------------------------------------------------------------------------

def test_merge_fills_empty_fields_only():
    primary = {
        "owner_name": "Jane",
        "owner_email": "",
        "owner_title": "",
        "owner_linkedin_url": "",
        "contact_emails": "a@b.com",
        "social_facebook": "https://facebook.com/a",
        "social_instagram": "",
        "social_linkedin": "",
    }
    fallback = {
        "owner_name": "Should-not-overwrite",
        "owner_email": "jane@biz.com",
        "owner_title": "",
        "owner_linkedin_url": "",
        "contact_emails": "info@biz.com",
        "social_facebook": "https://facebook.com/different",
        "social_instagram": "https://instagram.com/biz",
        "social_linkedin": "",
    }
    merged = merge_signals(primary, fallback)
    assert merged["owner_name"] == "Jane"  # primary wins when set
    assert merged["owner_email"] == "jane@biz.com"  # fallback fills empty
    assert merged["social_facebook"] == "https://facebook.com/a"  # primary wins
    assert merged["social_instagram"] == "https://instagram.com/biz"  # fallback fills
    # contact_emails is the union of both lists
    parts = merged["contact_emails"].split("; ")
    assert "a@b.com" in parts
    assert "info@biz.com" in parts


def test_merge_picks_owner_email_from_unioned_emails_when_primary_empty():
    """primary.owner_email empty, fallback only has generic; merge should
    pick the best of the union."""
    primary = dict.fromkeys(
        ("owner_name", "owner_email", "owner_title", "owner_linkedin_url",
         "contact_emails", "social_facebook", "social_instagram", "social_linkedin"),
        "",
    )
    primary["contact_emails"] = "info@biz.com"
    fallback = dict(primary)
    fallback["contact_emails"] = "jane@biz.com"
    merged = merge_signals(primary, fallback)
    # owner_email should be jane@ (personal) not info@ (generic)
    assert merged["owner_email"] == "jane@biz.com"


def test_merge_fills_business_description_from_fallback():
    """Homepage had no description; the /about page does -> merge fills it."""
    primary = extract_owner_signals("<html><h1>Home</h1></html>")
    fallback = extract_owner_signals(
        '<meta name="description" content="20 years of local roofing.">'
    )
    merged = merge_signals(primary, fallback)
    assert merged["business_description"] == "20 years of local roofing."


def test_merge_keeps_primary_business_description():
    primary = extract_owner_signals(
        '<meta name="description" content="Homepage blurb wins.">'
    )
    fallback = extract_owner_signals(
        '<meta name="description" content="About-page blurb loses.">'
    )
    merged = merge_signals(primary, fallback)
    assert merged["business_description"] == "Homepage blurb wins."


# ---------------------------------------------------------------------------
# enrich_from_page_with_fallback
# ---------------------------------------------------------------------------

def test_homepage_alone_when_owner_email_present():
    """When homepage yields an owner_email, no secondary fetch fires."""
    page = {"html": '<a href="mailto:jane@biz.com">Email</a>', "status_code": 200}
    fetch_calls: list[str] = []

    def _spy(base_url: str) -> str:
        fetch_calls.append(base_url)
        return ""

    sig = enrich_from_page_with_fallback(page, "https://biz.com",
                                         secondary_fetcher=_spy)
    assert sig["owner_email"] == "jane@biz.com"
    assert fetch_calls == []  # no secondary call


def test_secondary_pages_used_when_homepage_misses_owner_email():
    page = {"html": "<html>no contact here</html>", "status_code": 200}
    secondary = '<a href="mailto:owner@biz.com">Email</a>'

    def _spy(base_url: str) -> str:
        return secondary

    sig = enrich_from_page_with_fallback(page, "https://biz.com",
                                         secondary_fetcher=_spy)
    assert sig["owner_email"] == "owner@biz.com"
    assert "owner@biz.com" in sig["contact_emails"]


def test_no_secondary_html_returns_homepage_only():
    page = {"html": "<html>nothing</html>", "status_code": 200}

    def _spy(_base_url: str) -> str:
        return ""

    sig = enrich_from_page_with_fallback(page, "https://biz.com",
                                         secondary_fetcher=_spy)
    assert sig["owner_email"] == ""
    assert sig["contact_emails"] == ""


def test_secondary_fetcher_exception_is_swallowed():
    page = {"html": "<html>nothing</html>", "status_code": 200}

    def _boom(_base_url: str) -> str:
        raise RuntimeError("network melted")

    sig = enrich_from_page_with_fallback(page, "https://biz.com",
                                         secondary_fetcher=_boom)
    # Returns the homepage-only result without raising.
    assert sig["owner_email"] == ""


# ---------------------------------------------------------------------------
# Facebook handle extraction + About URL construction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fb_url,expected_handle", [
    ("https://www.facebook.com/SutterButtesRealEstate", "SutterButtesRealEstate"),
    ("https://facebook.com/Counihan-Family-Dentistry/", "Counihan-Family-Dentistry"),
    ("https://m.facebook.com/biz.handle", "biz.handle"),
    ("https://www.facebook.com/profile.php?id=123456789", "123456789"),
    ("https://www.facebook.com/pages/Some-Realty/987654321", "987654321"),
    ("https://www.facebook.com/biz/posts", "biz"),
    ("https://www.facebook.com/biz/photos", "biz"),
])
def test_facebook_handle_extraction(fb_url, expected_handle):
    assert _facebook_handle(fb_url) == expected_handle


def test_facebook_handle_invalid_url_returns_empty():
    assert _facebook_handle("https://example.com/notfacebook") == ""
    assert _facebook_handle("") == ""


def test_facebook_about_url_uses_mbasic():
    url = facebook_about_url("https://www.facebook.com/SutterButtesRealEstate")
    assert url == "https://mbasic.facebook.com/SutterButtesRealEstate/about"


def test_facebook_about_url_handles_numeric_id():
    url = facebook_about_url("https://www.facebook.com/profile.php?id=123456789")
    assert url == "https://mbasic.facebook.com/123456789/about"


def test_facebook_about_url_returns_empty_for_unparseable_url():
    assert facebook_about_url("https://example.com/notfb") == ""
    assert facebook_about_url("") == ""


# ---------------------------------------------------------------------------
# Facebook login-wall detection
# ---------------------------------------------------------------------------

def test_login_wall_detected_when_multiple_markers_present():
    html = """
    <form id="login_form">
      <input name="email">
      <input name="pass">
      <button class="loginbutton">Login</button>
      <a>Forgotten password</a>
    </form>
    """
    assert _is_facebook_login_wall(html) is True


def test_empty_html_treated_as_login_wall():
    assert _is_facebook_login_wall("") is True
    assert _is_facebook_login_wall(None) is True


def test_real_about_page_not_treated_as_login_wall():
    html = """
    <div>About Sutter Buttes Real Estate</div>
    <div>Address: 123 Main St, Yuba City CA</div>
    <div>Email: <a href="mailto:info@sbre.com">info@sbre.com</a></div>
    """
    assert _is_facebook_login_wall(html) is False


# ---------------------------------------------------------------------------
# extract_facebook_signals
# ---------------------------------------------------------------------------

def test_facebook_signals_extracted_from_about_page():
    html = """
    <div>About Page</div>
    <div>Email: <a href="mailto:owner@bizname.com">owner@bizname.com</a></div>
    <div>Phone: (530) 555-1234</div>
    <a href="https://www.instagram.com/biz_handle/">Follow us</a>
    """
    sig = extract_facebook_signals(html)
    assert sig["owner_email"] == "owner@bizname.com"
    assert sig["social_instagram"] == "https://www.instagram.com/biz_handle"
    # social_facebook should NOT echo back the page we came from
    assert sig["social_facebook"] == ""


def test_facebook_signals_returns_empty_for_login_wall():
    html = """
    <form id="login_form">
      <input name="email">
      <button class="loginbutton">Login</button>
      <a>Forgotten password</a>
    </form>
    <a href="mailto:should-not-appear@biz.com">x</a>
    """
    sig = extract_facebook_signals(html)
    assert sig["owner_email"] == ""
    assert sig["contact_emails"] == ""


def test_facebook_signals_returns_empty_for_no_html():
    assert all(v == "" for v in extract_facebook_signals(None).values())
    assert all(v == "" for v in extract_facebook_signals("").values())


# ---------------------------------------------------------------------------
# Full cascade: homepage → /contact + /about → FB About
# ---------------------------------------------------------------------------

def test_cascade_fb_only_fires_when_homepage_and_secondary_empty():
    """Homepage has no email, secondary has no email, FB has one → use FB."""
    page = {"html": '<a href="https://www.facebook.com/biz">FB</a>', "status_code": 200}
    secondary_calls: list[str] = []
    fb_calls: list[str] = []

    def _secondary(url: str) -> str:
        secondary_calls.append(url)
        return ""  # no luck on /contact or /about

    def _fb(url: str) -> str:
        fb_calls.append(url)
        return '<a href="mailto:owner@biz.com">Email us</a>'

    sig = enrich_from_page_with_fallback(
        page, "https://biz.com",
        secondary_fetcher=_secondary,
        facebook_fetcher=_fb,
    )
    assert secondary_calls == ["https://biz.com"]
    assert fb_calls == ["https://www.facebook.com/biz"]
    assert sig["owner_email"] == "owner@biz.com"


def test_cascade_fb_skipped_when_secondary_yields_email():
    """If /contact yields the email, FB stage doesn't fire."""
    page = {"html": '<a href="https://www.facebook.com/biz">FB</a>', "status_code": 200}
    fb_calls: list[str] = []

    def _secondary(_url: str) -> str:
        return '<a href="mailto:owner@biz.com">Email</a>'

    def _fb(url: str) -> str:
        fb_calls.append(url)
        return ""

    sig = enrich_from_page_with_fallback(
        page, "https://biz.com",
        secondary_fetcher=_secondary,
        facebook_fetcher=_fb,
    )
    assert sig["owner_email"] == "owner@biz.com"
    assert fb_calls == []  # FB never called


def test_cascade_fb_skipped_when_no_facebook_url():
    """No FB URL in the prospect's pages → FB stage doesn't fire."""
    page = {"html": "<html>nothing here</html>", "status_code": 200}
    fb_calls: list[str] = []

    def _secondary(_url: str) -> str:
        return ""

    def _fb(url: str) -> str:
        fb_calls.append(url)
        return ""

    sig = enrich_from_page_with_fallback(
        page, "https://biz.com",
        secondary_fetcher=_secondary,
        facebook_fetcher=_fb,
    )
    assert fb_calls == []


def test_cascade_fb_disabled_via_flag():
    """enable_facebook=False → FB stage never fires even with URL + missing data."""
    page = {"html": '<a href="https://facebook.com/biz">FB</a>', "status_code": 200}
    fb_calls: list[str] = []

    def _secondary(_url: str) -> str:
        return ""

    def _fb(url: str) -> str:
        fb_calls.append(url)
        return '<a href="mailto:owner@biz.com">x</a>'

    sig = enrich_from_page_with_fallback(
        page, "https://biz.com",
        secondary_fetcher=_secondary,
        facebook_fetcher=_fb,
        enable_facebook=False,
    )
    assert fb_calls == []
    assert sig["owner_email"] == ""


def test_cascade_fb_login_wall_swallowed():
    """When FB serves a login wall, cascade returns merged-so-far without raising."""
    page = {"html": '<a href="https://facebook.com/biz">FB</a>', "status_code": 200}

    def _secondary(_url: str) -> str:
        return ""

    def _fb(_url: str) -> str:
        # Login wall HTML
        return """
        <form id="login_form">
          <button class="loginbutton">Login</button>
          <a>Forgotten password</a>
        </form>
        """

    sig = enrich_from_page_with_fallback(
        page, "https://biz.com",
        secondary_fetcher=_secondary,
        facebook_fetcher=_fb,
    )
    # FB login wall yields no signals; owner_email stays empty but social_fb
    # extracted from homepage is preserved.
    assert sig["owner_email"] == ""
    assert "facebook.com/biz" in sig["social_facebook"]


def test_cascade_fb_fetcher_exception_swallowed():
    page = {"html": '<a href="https://facebook.com/biz">FB</a>', "status_code": 200}

    def _secondary(_url: str) -> str:
        return ""

    def _boom(_url: str) -> str:
        raise RuntimeError("FB blocked us")

    sig = enrich_from_page_with_fallback(
        page, "https://biz.com",
        secondary_fetcher=_secondary,
        facebook_fetcher=_boom,
    )
    # Returns merged-so-far without raising.
    assert sig["owner_email"] == ""
