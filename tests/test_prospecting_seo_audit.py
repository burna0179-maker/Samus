"""Tests for backend.prospecting.seo_audit.score_seo."""
from __future__ import annotations

from backend.prospecting.seo_audit import score_seo


def _page(html: str) -> dict:
    return {"final_url": "https://x", "status_code": 200, "html": html, "fetch_error": None}


def test_score_seo_no_html_returns_zero():
    score, issues = score_seo({"html": None})
    assert score == 0
    assert "no_html" in issues


def test_score_seo_perfect_page():
    html = """
    <html>
      <head>
        <title>Acme Roofing - Local SEO Champion</title>
        <meta name="description" content="Acme Roofing serves Yuba City CA">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
      </head>
      <body>
        <h1>Acme Roofing</h1>
        <p>Call us at (530) 555-1234. Located in Yuba City.</p>
      </body>
    </html>
    """
    score, issues = score_seo(_page(html))
    assert score == 100
    assert issues == []


def test_score_seo_missing_title():
    html = """
    <html><head>
      <meta name="description" content="ok">
      <meta name="viewport" content="width=device-width">
    </head><body><h1>X</h1><p>Call (530) 555-1234</p></body></html>
    """
    score, issues = score_seo(_page(html))
    assert "missing_title" in issues
    assert score < 100


def test_score_seo_missing_meta_description():
    html = """
    <html><head><title>X</title>
      <meta name="viewport" content="width=device-width">
    </head><body><h1>X</h1><p>Call (530) 555-1234</p></body></html>
    """
    _, issues = score_seo(_page(html))
    assert "missing_meta_description" in issues


def test_score_seo_missing_h1():
    html = """
    <html><head><title>X</title>
      <meta name="description" content="d">
      <meta name="viewport" content="width=device-width">
    </head><body><p>Call (530) 555-1234</p></body></html>
    """
    _, issues = score_seo(_page(html))
    assert "missing_h1" in issues


def test_score_seo_missing_mobile_viewport():
    html = """
    <html><head><title>X</title>
      <meta name="description" content="d">
    </head><body><h1>X</h1><p>Call (530) 555-1234</p></body></html>
    """
    _, issues = score_seo(_page(html))
    assert "missing_mobile_viewport" in issues


def test_score_seo_missing_phone_or_location():
    html = """
    <html><head><title>X</title>
      <meta name="description" content="d">
      <meta name="viewport" content="width=device-width">
    </head><body><h1>X</h1><p>just some text</p></body></html>
    """
    _, issues = score_seo(_page(html))
    assert "missing_phone_or_location" in issues


def test_score_seo_all_missing():
    score, issues = score_seo(_page("<html><body><p>blank</p></body></html>"))
    assert score == 0
    assert set(issues) == {
        "missing_title",
        "missing_meta_description",
        "missing_h1",
        "missing_mobile_viewport",
        "missing_phone_or_location",
    }


def test_score_seo_location_hint_satisfies_phone_check():
    html = """
    <html><head><title>X</title>
      <meta name="description" content="d">
      <meta name="viewport" content="width=device-width">
    </head><body><h1>X</h1><p>Located in Yuba City</p></body></html>
    """
    _, issues = score_seo(_page(html))
    assert "missing_phone_or_location" not in issues
