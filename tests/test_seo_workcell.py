"""Smoke tests for the seo workcell — audit + recommendations + content."""

from __future__ import annotations

import httpx


_GOOD_HTML = (
    "<!DOCTYPE html><html><head>"
    "<title>Acme Plumbing | Trusted 24/7 Service in Yuba City</title>"
    "<meta name='description' content='Acme Plumbing offers 24/7 emergency plumbing in Yuba City. Licensed, bonded, fair pricing. Call (530) 555-0102 today.'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    # Enrichment fixtures so this 'clean' page genuinely passes the
    # canonical / OG / schema / analytics checks added 2026-05-16.
    "<link rel='canonical' href='https://acme.example.com/'>"
    "<meta property='og:title' content='Acme Plumbing'>"
    "<meta property='og:description' content='24/7 plumbing in Yuba City'>"
    "<meta property='og:image' content='https://acme.example.com/og.jpg'>"
    "<script type='application/ld+json'>"
    '{"@context":"https://schema.org","@type":"Plumber",'
    '"name":"Acme Plumbing","telephone":"+1-530-555-0102",'
    '"address":{"@type":"PostalAddress","streetAddress":"123 Main",'
    '"addressLocality":"Yuba City","addressRegion":"CA"}}'
    "</script>"
    "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-ABC12345');</script>"
    "</head><body><h1>24/7 Emergency Plumbing in Yuba City</h1>"
    "<p>Visit us at 123 Main Street, Yuba City. Call (530) 555-0102.</p>"
    # One image with alt text + one internal link so alt-coverage + link-graph
    # checks pass for the clean fixture.
    "<img src='/logo.png' alt='Acme Plumbing logo'>"
    "<a href='/services/'>Our services</a>"
    "</body></html>"
)

_BAD_HTML = (
    "<!DOCTYPE html><html><head>"
    "<meta name='robots' content='noindex,nofollow'>"
    "</head><body>"
    "<img src='http://insecure.example.com/img.png'>"
    "<script src='http://insecure.example.com/x.js'></script>"
    "</body></html>"
)


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _patch_fetch(monkeypatch, html: str, status: int = 200):
    class _Resp:
        def __init__(self):
            self.text = html
            self.status_code = status
            self.headers = {"content-type": "text/html"}

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("bad", request=None, response=None)

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return _Resp()

    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod.httpx, "Client", _Client)


def test_audit_clean_page(monkeypatch):
    _patch_fetch(monkeypatch, _GOOD_HTML)
    from backend.seo.audit import audit_url

    result = audit_url(
        "https://acme.example.com", keywords=["plumbing yuba city"], industry="plumbing"
    )
    assert result.seo_score >= 90
    assert result.findings["fetched"] is True


def test_audit_bad_page_flags_issues(monkeypatch):
    _patch_fetch(monkeypatch, _BAD_HTML)
    from backend.seo.audit import audit_url

    result = audit_url("https://bad.example.com")
    issue_ids = {i.id for i in result.issues}
    assert "missing_title" in issue_ids
    assert "missing_h1" in issue_ids
    assert "mixed_content" in issue_ids
    assert "noindex_directive" in issue_ids
    assert result.seo_score < 100


def test_recommendations_for_each_issue():
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations

    audit = AuditResult(
        url="https://x.example",
        seo_score=50,
        issues=[
            SeoIssue(id="missing_title", severity="high", category="content", message="x"),
            SeoIssue(id="mixed_content", severity="critical", category="technical", message="x"),
        ],
        findings={"industry": "plumbing"},
        ts="2026-05-15T00:00:00Z",
    )
    recs, on_page = build_recommendations(audit, ["emergency plumbing"])
    assert len(recs) == 2
    # Critical sorts first
    assert recs[0].priority == 5
    assert "title" in on_page


def _raise_llm_unavailable(**kw):
    from backend.common.llm_client import LlmCallError

    raise LlmCallError("unavailable")


def test_content_fallback_no_llm(monkeypatch):
    import backend.seo.content as content_mod

    monkeypatch.setattr(content_mod, "anthropic_messages", _raise_llm_unavailable)
    from backend.seo.content import generate_content_drafts
    from backend.seo.models import OptimizeResult

    drafts, wc, used, _cost = generate_content_drafts(
        "https://x.example",
        OptimizeResult(
            url="https://x.example",
            recommendations=[],
            on_page_changes={"title": "X"},
            ts="2026-05-15T00:00:00Z",
        ),
        ["plumbing"],
        "professional",
        anthropic_api_key=None,
    )
    assert used is False
    assert wc > 0
    assert drafts["title"] == "X"


def test_service_full_pipeline(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    import backend.seo.content as content_mod

    monkeypatch.setattr(content_mod, "anthropic_messages", _raise_llm_unavailable)
    from backend.seo.models import AuditRequest, ContentRequest, OptimizeRequest
    from backend.seo.service import audit_site, generate_content, optimize_page

    audit = audit_site(AuditRequest(url="https://bad.example.com"))
    assert audit.seo_score < 100

    opt = optimize_page(
        OptimizeRequest(
            url=audit.url,
            audit_data=audit,
            target_keywords=["foo"],
        )
    )
    assert len(opt.recommendations) >= 1

    content = generate_content(
        ContentRequest(
            url=opt.url,
            optimization_data=opt,
            target_keywords=["foo"],
            tone="professional",
        )
    )
    assert content.used_llm is False
    assert content.word_count > 0
