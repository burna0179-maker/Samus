"""Tests for backend.seo.geo_strategy (GEO audit + citation strategy)."""
from __future__ import annotations

import pytest

from backend.seo.geo_strategy import (
    robots_txt_audit,
    schema_gap_audit,
    content_freshness_audit,
    topical_authority_gaps,
    geo_score,
    build_robots_txt_ai_block,
    run_geo_audit,
    GeoFinding,
    GeoAuditResult,
    TopicalClusterPlan,
    AI_CRAWLERS,
)
from backend.seo.models import AuditResult, SeoIssue
from backend.common.dates import iso_now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audit(schema_types: list[str] | None = None) -> AuditResult:
    return AuditResult(
        url="https://example.com",
        seo_score=80,
        issues=[],
        findings={
            "fetched": True,
            "schema_types": schema_types or [],
            "has_local_business_schema": False,
        },
        ts=iso_now(),
    )


# ---------------------------------------------------------------------------
# robots_txt_audit
# ---------------------------------------------------------------------------


def test_robots_txt_audit_fetch_failed_returns_medium_finding(monkeypatch):
    # Simulate fetch failure
    monkeypatch.setattr(
        "backend.seo.geo_strategy._fetch_robots_txt",
        lambda url: ("", "robots_txt"),
    )
    findings = robots_txt_audit("https://example.com")
    assert len(findings) == 1
    assert findings[0].severity == "medium"
    assert findings[0].id == "geo_robots_fetch_failed"


def test_robots_txt_audit_all_allowed_returns_pass(monkeypatch):
    # robots.txt that allows all
    txt = "User-agent: *\nAllow: /\n"
    monkeypatch.setattr(
        "backend.seo.geo_strategy._fetch_robots_txt",
        lambda url: (txt, "robots_txt"),
    )
    findings = robots_txt_audit("https://example.com")
    # Should have one pass finding
    assert any(f.severity == "pass" for f in findings)
    assert all(f.severity != "critical" for f in findings)


def test_robots_txt_audit_blocked_bot_returns_critical(monkeypatch):
    # Disallow OAI-SearchBot
    txt = "User-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    monkeypatch.setattr(
        "backend.seo.geo_strategy._fetch_robots_txt",
        lambda url: (txt, "robots_txt"),
    )
    findings = robots_txt_audit("https://example.com")
    critical = [f for f in findings if f.severity == "critical"]
    assert len(critical) >= 1
    ids = [f.id for f in critical]
    assert any("oai_searchbot" in fid for fid in ids)


def test_robots_txt_audit_findings_have_evidence_source(monkeypatch):
    txt = "User-agent: *\nAllow: /\n"
    monkeypatch.setattr(
        "backend.seo.geo_strategy._fetch_robots_txt",
        lambda url: (txt, "robots_txt"),
    )
    findings = robots_txt_audit("https://example.com")
    for f in findings:
        assert f.evidence_source == "robots_txt"


# ---------------------------------------------------------------------------
# schema_gap_audit
# ---------------------------------------------------------------------------


def test_schema_gap_audit_no_schemas_returns_highs():
    audit = _make_audit(schema_types=[])
    findings = schema_gap_audit(audit)
    severities = {f.id: f.severity for f in findings}
    assert severities.get("geo_no_faq_schema") == "high"
    assert severities.get("geo_no_article_schema") == "high"


def test_schema_gap_audit_has_faqpage_returns_pass():
    audit = _make_audit(schema_types=["FAQPage", "LocalBusiness"])
    findings = schema_gap_audit(audit)
    ids = [f.id for f in findings]
    assert "geo_faq_schema_present" in ids
    assert "geo_no_faq_schema" not in ids


def test_schema_gap_audit_has_article_schema_returns_pass():
    audit = _make_audit(schema_types=["Article", "FAQPage"])
    findings = schema_gap_audit(audit)
    ids = [f.id for f in findings]
    assert "geo_article_schema_present" in ids


def test_schema_gap_audit_all_findings_have_crawled_html_source():
    audit = _make_audit(schema_types=[])
    findings = schema_gap_audit(audit)
    for f in findings:
        assert f.evidence_source == "crawled_html"


# ---------------------------------------------------------------------------
# content_freshness_audit
# ---------------------------------------------------------------------------


def test_freshness_audit_no_html_no_schema_returns_high():
    audit = _make_audit(schema_types=[])
    findings = content_freshness_audit(audit, html="")
    assert any(f.id == "geo_content_stale" for f in findings)
    stale = next(f for f in findings if f.id == "geo_content_stale")
    assert stale.severity == "high"


def test_freshness_audit_visible_date_in_html_reduces_severity():
    audit = _make_audit(schema_types=[])
    html = '<p>Last updated: June 10, 2026</p>'
    findings = content_freshness_audit(audit, html=html)
    ids = [f.id for f in findings]
    # visible date found but no schema -- should be medium
    assert "geo_content_stale" not in ids
    assert "geo_no_date_modified_schema" in ids


def test_freshness_audit_date_modified_schema_in_html_returns_pass():
    audit = _make_audit(schema_types=[])
    html = '{"@type":"Article","dateModified":"2026-06-01"}'
    findings = content_freshness_audit(audit, html=html)
    assert any(f.id == "geo_freshness_ok" for f in findings)


def test_freshness_audit_evidence_source_is_crawled_html():
    audit = _make_audit(schema_types=[])
    findings = content_freshness_audit(audit, html="")
    for f in findings:
        assert f.evidence_source == "crawled_html"


# ---------------------------------------------------------------------------
# topical_authority_gaps
# ---------------------------------------------------------------------------


def test_topical_authority_gaps_returns_ten_articles():
    plan = topical_authority_gaps("GEO optimization", "plumbing")
    assert isinstance(plan, TopicalClusterPlan)
    assert len(plan.articles) == 10


def test_topical_authority_gaps_articles_are_questions():
    plan = topical_authority_gaps("local SEO", "dental")
    for article in plan.articles:
        # Must be a question (ends with ?) or starts with a question word
        assert "?" in article or any(
            article.startswith(w) for w in ("How", "What", "Why", "Which", "When", "Where")
        )


def test_topical_authority_gaps_secondary_kws_applied():
    plan = topical_authority_gaps("SEO", "plumbing", secondary_kws=["schema markup", "AI citation"])
    # Last 2 articles should reference secondary keywords
    joined = " ".join(plan.articles[-2:])
    assert "schema markup" in joined or "AI citation" in joined


# ---------------------------------------------------------------------------
# geo_score
# ---------------------------------------------------------------------------


def test_geo_score_no_issues_is_100():
    assert geo_score([]) == 100


def test_geo_score_critical_deducts_15():
    f = GeoFinding(
        id="x", severity="critical", category="robots_txt",
        message="", recommendation="",
    )
    assert geo_score([f]) == 85


def test_geo_score_high_deducts_10():
    f = GeoFinding(
        id="x", severity="high", category="schema",
        message="", recommendation="",
    )
    assert geo_score([f]) == 90


def test_geo_score_floor_is_zero():
    findings = [
        GeoFinding(id=f"f{i}", severity="critical", category="schema",
                   message="", recommendation="")
        for i in range(20)
    ]
    assert geo_score(findings) == 0


def test_geo_score_pass_findings_ignored():
    findings = [
        GeoFinding(id="p", severity="pass", category="schema",
                   message="", recommendation=""),
        GeoFinding(id="h", severity="high", category="schema",
                   message="", recommendation=""),
    ]
    assert geo_score(findings) == 90


# ---------------------------------------------------------------------------
# build_robots_txt_ai_block
# ---------------------------------------------------------------------------


def test_build_robots_txt_ai_block_contains_all_crawlers():
    snippet = build_robots_txt_ai_block()
    for bot in AI_CRAWLERS:
        assert bot in snippet


def test_build_robots_txt_ai_block_is_ascii():
    snippet = build_robots_txt_ai_block()
    assert snippet.isascii()


def test_build_robots_txt_ai_block_has_allow_lines():
    snippet = build_robots_txt_ai_block()
    assert "Allow: /" in snippet


# ---------------------------------------------------------------------------
# schema_builder.build_robots_txt_ai_block (duplicate from schema_builder)
# ---------------------------------------------------------------------------


def test_schema_builder_robots_block_contains_crawlers():
    from backend.seo.schema_builder import build_robots_txt_ai_block as sb_block
    snippet = sb_block()
    for bot in ("OAI-SearchBot", "PerplexityBot", "Claude-SearchBot"):
        assert bot in snippet


def test_schema_builder_build_article_schema():
    from backend.seo.schema_builder import build_article_schema
    schema = build_article_schema(
        headline="How to Optimize for AI Citation",
        author_name="Alex Smith",
        publisher_name="Hustleforge",
        date_published="2026-06-11",
        date_modified="2026-06-11",
    )
    assert schema["@type"] == "Article"
    assert schema["datePublished"] == "2026-06-11"
    assert schema["dateModified"] == "2026-06-11"
    assert schema["author"]["@type"] == "Person"
    assert schema["author"]["name"] == "Alex Smith"


# ---------------------------------------------------------------------------
# run_geo_audit integration
# ---------------------------------------------------------------------------


def test_run_geo_audit_returns_geo_audit_result(monkeypatch):
    # Patch fetch to avoid real HTTP
    monkeypatch.setattr(
        "backend.seo.geo_strategy._fetch_robots_txt",
        lambda url: ("User-agent: *\nAllow: /\n", "robots_txt"),
    )
    audit = _make_audit(schema_types=[])
    result = run_geo_audit(audit, primary_kw="GEO", industry="plumbing")
    assert isinstance(result, GeoAuditResult)
    assert 0 <= result.geo_score <= 100
    assert result.topical_plan is not None
    assert len(result.topical_plan.articles) == 10
    assert result.robots_txt_snippet


def test_run_geo_audit_score_lower_with_missing_schemas(monkeypatch):
    monkeypatch.setattr(
        "backend.seo.geo_strategy._fetch_robots_txt",
        lambda url: ("User-agent: *\nAllow: /\n", "robots_txt"),
    )
    audit_no_schema = _make_audit(schema_types=[])
    audit_full_schema = _make_audit(schema_types=["FAQPage", "Article", "LocalBusiness"])

    result_no = run_geo_audit(audit_no_schema, primary_kw="SEO")
    result_full = run_geo_audit(audit_full_schema, primary_kw="SEO")
    assert result_full.geo_score > result_no.geo_score
