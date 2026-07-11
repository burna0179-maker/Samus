"""SEO enrichment extractors (added 2026-05-16): canonical / Open Graph /
schema.org JSON-LD / alt-text coverage / link graph / analytics / lazy-load.

Each extractor is tested in isolation against tightly-scoped HTML fixtures
so failures point at one piece of the parsing chain at a time. The
integration paths (audit_url + _build_issues) are covered by the broader
smoke tests in test_seo_workcell.py + test_seo_deeper.py.
"""
from __future__ import annotations

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers (same shape as the existing seo test fixtures)
# ---------------------------------------------------------------------------

def _patch_fetch(monkeypatch, html: str, status: int = 200):
    class _Resp:
        def __init__(self):
            self.text = html
            self.status_code = status
            self.headers = {"content-type": "text/html"}

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            return _Resp()

    import backend.seo.audit as audit_mod
    monkeypatch.setattr(audit_mod.httpx, "Client", _Client)


def _audit(monkeypatch, html: str):
    _patch_fetch(monkeypatch, html)
    from backend.seo.audit import audit_url
    return audit_url("https://example.com/")


def _issue_ids(result) -> set[str]:
    return {i.id for i in result.issues}


# ---------------------------------------------------------------------------
# Canonical URL
# ---------------------------------------------------------------------------

def test_canonical_extracted_from_link_rel(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://canonical.example.com/page'>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["canonical_url"] == "https://canonical.example.com/page"
    assert "missing_canonical" not in _issue_ids(result)


def test_missing_canonical_flagged_as_high():
    from backend.seo.audit import _build_enrichment_issues
    from backend.seo.models import SeoIssue
    parsed = {"canonical_url": "", "og": {"title": "x", "image": "y"},
              "schema_types": ["Plumber"], "has_local_business_schema": True,
              "has_organization_schema": False, "image_count": 1,
              "images_with_alt": 1, "link_internal_count": 1,
              "link_external_count": 0, "has_ga4": True, "has_gtm": False,
              "has_meta_pixel": False, "has_legacy_ga": False}
    issues: list[SeoIssue] = []
    _build_enrichment_issues("https://x.example.com/", parsed, issues)
    by_id = {i.id: i for i in issues}
    assert "missing_canonical" in by_id
    assert by_id["missing_canonical"].severity == "high"
    assert by_id["missing_canonical"].category == "technical"


# ---------------------------------------------------------------------------
# Open Graph
# ---------------------------------------------------------------------------

def test_open_graph_keys_stripped_of_og_prefix(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='OG Title'>"
        "<meta property='og:description' content='OG Desc'>"
        "<meta property='og:image' content='https://example.com/i.png'>"
        "<meta property='og:type' content='website'>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    og = result.findings["og"]
    assert og["title"] == "OG Title"
    assert og["description"] == "OG Desc"
    assert og["image"] == "https://example.com/i.png"
    assert og["type"] == "website"
    assert "missing_og_title" not in _issue_ids(result)
    assert "missing_og_image" not in _issue_ids(result)


def test_missing_og_title_and_image_both_flagged(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        # no og:title, no og:image
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    ids = _issue_ids(result)
    assert "missing_og_title" in ids
    assert "missing_og_image" in ids


# ---------------------------------------------------------------------------
# schema.org JSON-LD
# ---------------------------------------------------------------------------

def test_schema_org_local_business_recognized(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>"
        '{"@context":"https://schema.org","@type":"Plumber","name":"X"}'
        "</script>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    assert "Plumber" in result.findings["schema_types"]
    assert result.findings["has_local_business_schema"] is True
    assert "missing_schema_org" not in _issue_ids(result)
    assert "missing_local_business_schema" not in _issue_ids(result)


def test_schema_org_graph_wrapped_normalization(monkeypatch):
    """Some CMSes emit JSON-LD wrapped in @graph: [...] — both shapes parse."""
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>"
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"WebSite","name":"X"},'
        '{"@type":"Restaurant","name":"X"}'
        "]}"
        "</script>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    types = result.findings["schema_types"]
    assert "Restaurant" in types
    assert "WebSite" in types
    assert result.findings["has_local_business_schema"] is True


def test_schema_org_present_but_no_local_business_flagged(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>"
        '{"@context":"https://schema.org","@type":"WebSite","name":"X"}'
        "</script>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    ids = _issue_ids(result)
    assert "missing_schema_org" not in ids  # JSON-LD IS present
    assert "missing_local_business_schema" in ids  # but no LB type


def test_schema_org_malformed_block_skipped_not_crashing(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>NOT VALID JSON</script>"
        "<script type='application/ld+json'>"
        '{"@type":"Plumber"}'
        "</script>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    # The malformed block is silently dropped; the valid block survives.
    assert "Plumber" in result.findings["schema_types"]


# ---------------------------------------------------------------------------
# Alt-text coverage
# ---------------------------------------------------------------------------

def test_alt_text_coverage_calculated(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>"
        '{"@type":"Plumber"}'
        "</script>"
        "<script>gtag('config','G-ABC12345');</script>"
        "</head><body><h1>h</h1>"
        "<img src='a.png' alt='a'><img src='b.png' alt='b'>"
        "<img src='c.png'>"  # no alt → 2/3 = 66.7%
        "<a href='/'>internal</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["image_count"] == 3
    assert result.findings["images_with_alt"] == 2
    assert result.findings["alt_coverage_pct"] == pytest.approx(66.7, abs=0.1)
    # 66.7% is < 80% → medium issue
    assert "low_alt_text_coverage" in _issue_ids(result)


def test_alt_text_coverage_below_50pct_is_high_severity(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script>gtag('config','G-ABC12345');</script>"
        "</head><body><h1>h</h1>"
        "<img src='a.png'><img src='b.png'><img src='c.png'><img src='d.png' alt='d'>"
        "<a href='/'>internal</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    ids = _issue_ids(result)
    assert "low_alt_text_coverage_critical" in ids
    assert "low_alt_text_coverage" not in ids  # not double-counted


# ---------------------------------------------------------------------------
# Link graph
# ---------------------------------------------------------------------------

def test_link_graph_internal_vs_external(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script>gtag('config','G-ABC12345');</script>"
        "</head><body><h1>h</h1>"
        "<img src='a.png' alt='a'>"
        "<a href='/services/'>Services</a>"
        "<a href='/contact'>Contact</a>"
        "<a href='https://other.example.com/'>External</a>"
        "<a href='https://example.com/about/' rel='nofollow'>About (nofollow)</a>"
        "<a href='#top'>Skip-link (ignored)</a>"
        "<a href='mailto:x@y.com'>Email (ignored)</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    f = result.findings
    # 3 internal (services, contact, about) + 1 external (other.example.com)
    assert f["link_internal_count"] == 3
    assert f["link_external_count"] == 1
    assert f["link_nofollow_count"] == 1
    assert f["unique_anchor_count"] >= 3  # at least Services, Contact, External


def test_no_internal_links_flagged(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script>gtag('config','G-ABC12345');</script>"
        "</head><body><h1>h</h1>"
        "<img src='a.png' alt='a'>"
        "<a href='https://other.example.com/'>External only</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    assert "no_internal_links" in _issue_ids(result)


# ---------------------------------------------------------------------------
# Analytics detection
# ---------------------------------------------------------------------------

def test_no_analytics_flagged(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        # No GA / GTM / Pixel
        "</head><body><h1>h</h1>"
        "<img src='a.png' alt='a'><a href='/'>i</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["has_ga4"] is False
    assert result.findings["has_gtm"] is False
    assert "no_analytics_detected" in _issue_ids(result)


def test_ga4_recognized_via_gtag_snippet(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script async src='https://www.googletagmanager.com/gtag/js?id=G-ABCDEF1234'></script>"
        "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}gtag('js',new Date());gtag('config','G-ABCDEF1234');</script>"
        "</head><body><h1>h</h1>"
        "<img src='a.png' alt='a'><a href='/'>i</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["has_ga4"] is True
    assert "no_analytics_detected" not in _issue_ids(result)
    assert "legacy_analytics_only" not in _issue_ids(result)


def test_gtm_recognized_via_googletagmanager_src(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script async src='https://www.googletagmanager.com/gtm.js?id=GTM-ABCDEF1'></script>"
        "</head><body><h1>h</h1><img src='a.png' alt='a'><a href='/'>i</a></body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["has_gtm"] is True
    assert "no_analytics_detected" not in _issue_ids(result)


def test_legacy_ga_only_flagged(monkeypatch):
    """UA-only sites should get the upgrade-to-GA4 warning."""
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script>ga('create','UA-12345678-1','auto');</script>"
        "</head><body><h1>h</h1><img src='a.png' alt='a'><a href='/'>i</a></body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["has_legacy_ga"] is True
    assert result.findings["has_ga4"] is False
    ids = _issue_ids(result)
    assert "legacy_analytics_only" in ids
    assert "no_analytics_detected" not in ids


def test_meta_pixel_recognized(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script>!function(f,b,e,v,n,t,s){fbq('init','1234567890');}(window);</script>"
        "</head><body><h1>h</h1><img src='a.png' alt='a'><a href='/'>i</a></body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["has_meta_pixel"] is True
    assert "no_analytics_detected" not in _issue_ids(result)


# ---------------------------------------------------------------------------
# Lazy-loading
# ---------------------------------------------------------------------------

def test_lazy_load_counted(monkeypatch):
    html = (
        "<html><head>"
        "<title>x</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/'>"
        "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
        "<script type='application/ld+json'>{\"@type\":\"Plumber\"}</script>"
        "<script>gtag('config','G-ABC12345');</script>"
        "</head><body><h1>h</h1>"
        "<img src='1.png' alt='1' loading='lazy'>"
        "<img src='2.png' alt='2' loading='lazy'>"
        "<img src='3.png' alt='3'>"
        "<a href='/'>i</a>"
        "</body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["image_count"] == 3
    assert result.findings["images_with_lazy_load"] == 2


# ---------------------------------------------------------------------------
# Regex fallback (bs4 disabled) — minimal smoke
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Recommendations coverage for enrichment issue IDs
# ---------------------------------------------------------------------------

ENRICHMENT_ISSUE_IDS = [
    "missing_canonical",
    "missing_og_title",
    "missing_og_image",
    "missing_schema_org",
    "missing_local_business_schema",
    "low_alt_text_coverage",
    "low_alt_text_coverage_critical",
    "no_internal_links",
    "no_analytics_detected",
    "legacy_analytics_only",
]


@pytest.mark.parametrize("issue_id", ENRICHMENT_ISSUE_IDS)
def test_every_enrichment_issue_has_a_dedicated_recommendation(issue_id):
    """Each new enrichment issue must map to a non-generic recommendation
    in recommendations._recommendation_for. Without this guard, a new
    issue ID added to audit.py would silently fall back to the generic
    'Resolve <id>' message in the customer report."""
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations
    audit = AuditResult(
        url="https://x.example/", seo_score=50,
        issues=[SeoIssue(id=issue_id, severity="high", category="technical",
                         message="probe")],
        findings={"industry": "plumbing", "title": "X"},
        ts="2026-05-16T00:00:00Z",
    )
    recs, _on_page = build_recommendations(audit, ["plumbing yuba city"])
    assert len(recs) == 1
    rec = recs[0]
    # Generic-fallback action is "Resolve <id>"; the dedicated path
    # always writes a longer, prospect-readable action.
    assert not rec.action.lower().startswith(f"resolve {issue_id}"), (
        f"{issue_id} fell through to the generic recommendation"
    )
    # Rationale must explain WHY in plain language (>40 chars rules out
    # the auto-fallback "<severity> severity issue in <category>")
    assert len(rec.rationale) > 40


def test_enrichment_recommendations_produce_on_page_suggestions():
    """The most actionable enrichment issues (canonical, OG, schema) should
    contribute concrete paste-ready snippets to on_page_changes."""
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations
    audit = AuditResult(
        url="https://x.example/", seo_score=50,
        issues=[
            SeoIssue(id="missing_canonical", severity="high",
                     category="technical", message="x"),
            SeoIssue(id="missing_og_title", severity="medium",
                     category="content", message="x"),
            SeoIssue(id="missing_og_image", severity="medium",
                     category="content", message="x"),
            SeoIssue(id="missing_schema_org", severity="high",
                     category="technical", message="x"),
        ],
        findings={"industry": "plumbing", "title": "Acme Plumbing"},
        ts="2026-05-16T00:00:00Z",
    )
    _recs, on_page = build_recommendations(audit, ["plumbing"])
    assert "canonical_link" in on_page
    assert "https://x.example/" in on_page["canonical_link"]
    assert "og_title" in on_page
    assert "og_image" in on_page
    assert "schema_org_jsonld" in on_page
    # Schema scaffold must reference LocalBusiness so operator knows it's
    # the right starting type for a service business.
    assert "LocalBusiness" in on_page["schema_org_jsonld"]


def test_regex_fallback_extracts_canonical_and_og(monkeypatch):
    """When bs4 is unavailable, the regex path still pulls canonical + OG."""
    import backend.seo.audit as audit_mod
    monkeypatch.setattr(audit_mod, "_HAS_BS4", False)
    html = (
        "<html><head>"
        "<title>Acme</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://example.com/page'>"
        "<meta property='og:title' content='OG'>"
        "<meta property='og:image' content='https://example.com/i.png'>"
        "<script type='application/ld+json'>{\"@type\":\"Restaurant\"}</script>"
        "<script>gtag('config','G-ABC');</script>"
        "</head><body><h1>h</h1></body></html>"
    )
    result = _audit(monkeypatch, html)
    assert result.findings["parser"] == "regex"
    assert result.findings["canonical_url"] == "https://example.com/page"
    assert result.findings["og"]["title"] == "OG"
    assert result.findings["og"]["image"] == "https://example.com/i.png"
    assert "Restaurant" in result.findings["schema_types"]
    assert result.findings["has_local_business_schema"] is True
    assert result.findings["has_ga4"] is True
    # Structural extractors (alt-text, link graph) NOT run under regex path,
    # so missing-internal-link / low-alt issues must NOT fire.
    assert "no_internal_links" not in _issue_ids(result)
    assert "low_alt_text_coverage" not in _issue_ids(result)
    assert "low_alt_text_coverage_critical" not in _issue_ids(result)
