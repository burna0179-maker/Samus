"""Jurisdiction-aware legal page generation."""

from __future__ import annotations

from backend.website import legal
from backend.website.models import WebsiteBrief


def _brief(**over):
    base = dict(
        business_name="Sample Cleaning",
        business_description="House cleaning.",
        contact_email="hello@mighty.test",
        address="<street>, <city>, <state> 97624",
    )
    base.update(over)
    return WebsiteBrief(**base)


def test_resolve_us_state_ca_adds_ccpa():
    j = legal.resolve_jurisdiction("US", "CA")
    assert "CCPA/CPRA" in " ".join(j.laws)
    assert "opt-out of sale/sharing" in j.rights
    assert "State of CA" in j.governing_law


def test_resolve_us_federal_baseline_always_present():
    j = legal.resolve_jurisdiction("US", "OR")
    laws = " ".join(j.laws)
    assert "ADA" in laws and "CAN-SPAM" in laws and "COPPA" in laws


def test_resolve_eu_requires_cookie_consent_and_gdpr():
    j = legal.resolve_jurisdiction("DE", "")
    assert j.cookie_consent is True
    assert "GDPR" in " ".join(j.laws)
    assert "erasure" in j.rights


def test_resolve_unknown_country_flagged_and_conservative():
    j = legal.resolve_jurisdiction("ZZ", "")
    assert j.flagged_unknown is True
    assert j.cookie_consent is True  # conservative default


def test_resolve_unknown_us_state_flagged():
    j = legal.resolve_jurisdiction("US", "PR")
    assert j.flagged_unknown is True
    assert "State of PR" in j.governing_law


def test_privacy_policy_covers_required_sections():
    j = legal.resolve_jurisdiction("US", "CA")
    html = legal.privacy_policy_html(_brief(), j)
    for section in (
        "Privacy Policy",
        "Information We Collect",
        "Your Rights",
        "Children",
        "Cookies",
        "Contact Us",
    ):  # headings are HTML-escaped
        assert section in html
    assert "hello@mighty.test" in html
    assert "CCPA" in html  # applicable framework noted


def test_terms_covers_required_sections_and_governing_law():
    j = legal.resolve_jurisdiction("US", "OR")
    html = legal.terms_html(_brief(), j)
    for section in (
        "Conditions",
        "Limitation of Liability",
        "Governing Law",
        "Intellectual Property",
        "Disclaimers",
    ):  # "&" is HTML-escaped
        assert section in html
    assert "State of OR" in html


def test_compliance_report_has_disclaimer_and_obligations():
    j = legal.resolve_jurisdiction("US", "OR")
    rep = legal.compliance_report(_brief(), j)
    assert "not legal advice" in rep["disclaimer"]
    assert any("CAN-SPAM" in o for o in rep["obligations"])  # email -> CAN-SPAM flagged
    assert rep["applicable_laws"]


def test_compliance_eu_flags_cookie_banner():
    j = legal.resolve_jurisdiction("FR", "")
    rep = legal.compliance_report(_brief(), j)
    assert any("Cookie consent banner" in f for f in rep["flags"])


def test_disclaimer_covers_industry_standard_sections():
    j = legal.resolve_jurisdiction("US", "OR")
    html = legal.disclaimer_html(_brief(), j)
    for section in (
        "Disclaimer",
        "No Guarantee of Results",
        "Quotes and Pricing",
        "Images and Examples",
        "External Links",
        "Limitation of Liability",
        "Contact",
    ):
        assert section in html
    assert "hello@mighty.test" in html


def test_no_em_dash_in_legal_output():
    j = legal.resolve_jurisdiction("US", "OR")
    blob = (
        legal.privacy_policy_html(_brief(), j)
        + legal.terms_html(_brief(), j)
        + legal.disclaimer_html(_brief(), j)
    )
    assert "—" not in blob and "–" not in blob
