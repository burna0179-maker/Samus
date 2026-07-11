"""Tests for backend.seo.schema_builder — JSON-LD generation (pure)."""
from __future__ import annotations

import json

from backend.seo import schema_builder as sb


def test_faq_page_shape_and_drops_empty():
    faq = [
        {"q": "What is X?", "a": "X is a thing."},
        {"q": "", "a": "orphan answer"},  # dropped — no question
        {"q": "Empty?", "a": ""},  # dropped — no answer
    ]
    schema = sb.faq_page(faq)
    assert schema["@type"] == "FAQPage"
    assert schema["@context"] == "https://schema.org"
    assert len(schema["mainEntity"]) == 1
    q = schema["mainEntity"][0]
    assert q["@type"] == "Question"
    assert q["acceptedAnswer"]["@type"] == "Answer"
    assert q["acceptedAnswer"]["text"] == "X is a thing."


def test_article_truncates_headline_and_cleans():
    long_headline = "A" * 200
    schema = sb.article(headline=long_headline, description="d", url="https://e.com/p", author="Acme", image="https://e.com/i.png")
    assert len(schema["headline"]) <= 110
    assert schema["@type"] == "Article"
    assert schema["mainEntityOfPage"]["@id"] == "https://e.com/p"
    assert schema["author"]["name"] == "Acme"
    # dateModified defaults to datePublished when absent — both empty here -> dropped
    assert "dateModified" not in schema


def test_article_date_modified_defaults_to_published():
    schema = sb.article(headline="h", date_published="2026-01-01")
    assert schema["datePublished"] == "2026-01-01"
    assert schema["dateModified"] == "2026-01-01"


def test_organization_drops_empties():
    schema = sb.organization(name="Acme", url="https://acme.com", same_as=[])
    assert schema["@type"] == "Organization"
    assert "sameAs" not in schema  # empty list dropped
    assert "telephone" not in schema


def test_local_business_nested_address():
    schema = sb.local_business(
        name="Joe's Plumbing",
        business_type="Plumber",
        city="Yuba City",
        region="CA",
        telephone="555-1234",
    )
    assert schema["@type"] == "Plumber"
    assert schema["address"]["@type"] == "PostalAddress"
    assert schema["address"]["addressLocality"] == "Yuba City"
    assert "streetAddress" not in schema["address"]  # empty dropped


def test_how_to_positions_steps():
    schema = sb.how_to(name="Fix a leak", steps=["Shut water", "", "Replace seal"])
    assert schema["@type"] == "HowTo"
    assert len(schema["step"]) == 2  # empty step dropped
    assert schema["step"][0]["position"] == 1
    assert schema["step"][1]["position"] == 2


def test_breadcrumb_positions():
    schema = sb.breadcrumb([{"name": "Home", "url": "https://e.com"}, {"name": "Blog", "url": "https://e.com/blog"}])
    assert schema["itemListElement"][1]["position"] == 2
    assert schema["itemListElement"][0]["name"] == "Home"


def test_to_script_tag_escapes_and_parses():
    schema = sb.faq_page([{"q": "Is 1 < 2?", "a": "Yes, 1 < 2."}])
    tag = sb.to_script_tag(schema)
    assert tag.startswith('<script type="application/ld+json">')
    assert tag.rstrip().endswith("</script>")
    # raw "<" inside the payload is escaped so it can't break out of the script
    inner = tag.split(">", 1)[1].rsplit("<", 1)[0]
    assert "\\u003c" in inner
    # and the escaped JSON still parses back to the same object
    assert json.loads(inner.replace("\\u003c", "<")) == schema
