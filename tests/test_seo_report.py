"""Tests for the SEO customer-facing markdown report."""
from __future__ import annotations

import json

import httpx

from backend.seo.models import (
    AuditResult,
    ContentResult,
    OptimizationRecommendation,
    OptimizeResult,
    SeoIssue,
)
from backend.seo.report import (
    customer_slug_from_url,
    render_seo_report_markdown,
    write_seo_report,
)


# Reuse the proven _FakeHttpx + patch helpers from the deeper suite.
from tests.test_seo_deeper import (  # type: ignore[import-untyped]
    _BAD_HTML,
    _patch_anthropic,
    _patch_audit_fetch,
    _reset_idempotency,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

def _five_issue_audit() -> AuditResult:
    # G6 evidence_source tags: every finding here is detected by a
    # deterministic HTML/meta parser, so each is tagged with crawled_html
    # or crawled_meta. None are LLM-inferred. Without tags the G6
    # serialization filter would drop them.
    issues = [
        SeoIssue(id="mixed_content", severity="critical", category="technical",
                 message="HTTPS page loads HTTP sub-resources (mixed content).",
                 evidence="http://x.example/a.png",
                 evidence_source="crawled_html"),
        SeoIssue(id="noindex_directive", severity="critical", category="technical",
                 message="Page explicitly opts out of indexing (noindex).",
                 evidence_source="crawled_meta"),
        SeoIssue(id="missing_title", severity="high", category="content",
                 message="Page is missing a <title> tag.",
                 evidence_source="crawled_meta"),
        SeoIssue(id="missing_h1", severity="high", category="content",
                 message="Page has no <h1> heading.",
                 evidence_source="crawled_html"),
        SeoIssue(id="missing_local_signals", severity="medium", category="local",
                 message="Page has no visible phone or address tokens.",
                 evidence_source="crawled_html"),
    ]
    return AuditResult(
        url="https://acme.example.com",
        seo_score=70,
        issues=issues,
        findings={"industry": "plumbing", "keywords": ["plumbing yuba"]},
        ts="2026-05-15T00:00:00Z",
    )


def _matching_optimize(audit: AuditResult) -> OptimizeResult:
    recs = [
        OptimizationRecommendation(
            area="technical", action="Migrate HTTP sub-resources to HTTPS",
            rationale="Mixed content blocks indexing.", priority=5,
        ),
        OptimizationRecommendation(
            area="title", action="Add a <title> tag with the primary keyword",
            rationale="Title tags are the highest-impact on-page signal.", priority=4,
        ),
        OptimizationRecommendation(
            area="local", action="Add visible phone number and street address",
            rationale="Local pack ranking requires NAP signals.", priority=3,
        ),
    ]
    on_page = {
        "title": "Plumbing Yuba | Trusted Plumbing Services",
        "h1": "Plumbing Yuba You Can Trust",
    }
    return OptimizeResult(
        url=audit.url, recommendations=recs, on_page_changes=on_page,
        ts="2026-05-15T00:00:00Z",
    )


def _full_content() -> ContentResult:
    return ContentResult(
        url="https://acme.example.com",
        page_drafts={
            "title": "Acme Plumbing | Trusted 24/7 Yuba City",
            "meta_description": "Acme Plumbing offers 24/7 plumbing services with transparent pricing.",
            "h1": "Plumbing You Can Trust",
            "body_intro": "We deliver fast, honest plumbing service in Yuba City.",
            "body_main": "We handle drains, water heaters, and emergencies. " * 8,
            "cta": "Call us today for a free consultation.",
        },
        word_count=120,
        used_llm=True,
        ts="2026-05-15T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------

def test_render_with_bad_audit_includes_all_issues():
    audit = _five_issue_audit()
    optimize = _matching_optimize(audit)
    body = render_seo_report_markdown(audit, optimize, None)
    # Every issue id should appear in the body.
    for issue in audit.issues:
        assert issue.id in body, f"issue id {issue.id} missing from report"
    # Severity badges should be present.
    assert "[CRITICAL]" in body
    assert "[HIGH]" in body
    assert "[MEDIUM]" in body
    # The top recommendation should be marked.
    assert "RECOMMENDED" in body
    # Score should appear in the cover.
    assert "70" in body
    # No em-dashes anywhere.
    assert "—" not in body
    assert "–" not in body


def test_render_with_no_content_section_omits_content_block():
    audit = _five_issue_audit()
    optimize = _matching_optimize(audit)
    body = render_seo_report_markdown(audit, optimize, None)
    assert "Content Drafts" not in body


def test_render_with_content_includes_drafts():
    audit = _five_issue_audit()
    optimize = _matching_optimize(audit)
    content = _full_content()
    body = render_seo_report_markdown(audit, optimize, content)
    assert "Content Drafts" in body
    for field in ("title", "meta_description", "h1", "body_intro", "body_main", "cta"):
        # Each draft value should appear inside a fenced code block.
        value = content.page_drafts[field]
        # body_main is long; check a substring.
        snippet = value.splitlines()[0][:40] if value else value
        assert snippet in body, f"draft field {field} value not found in report"
    # Count fenced code blocks: should be at least 6 (one per draft field).
    assert body.count("```") >= 12  # opening + closing per draft = 12


def test_customer_slug_from_url_examples():
    assert customer_slug_from_url("https://www.acme-plumbing.example.com/") == \
        "acme-plumbing_example_com"
    assert customer_slug_from_url("http://yuba-pizza.com") == "yuba-pizza_com"
    assert customer_slug_from_url("https://ACME.COM/foo?bar=1") == "acme_com"
    # Edge cases: hostnameless input should not crash.
    assert customer_slug_from_url("not-a-url") != ""


def test_write_seo_report_creates_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    audit = _five_issue_audit()
    optimize = _matching_optimize(audit)
    content = _full_content()
    path = write_seo_report(audit, optimize, content)
    assert path.exists()
    assert path.stat().st_size > 0
    assert path.name == "seo_report.md"
    assert path.parent.parent.name == "customers"
    assert path.parent.name == "acme_example_com"
    text = path.read_text(encoding="utf-8")
    assert "SEO Audit & Fix Report" in text
    assert "mixed_content" in text


def test_write_seo_report_uses_explicit_customer_label(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    audit = _five_issue_audit()
    optimize = _matching_optimize(audit)
    path = write_seo_report(audit, optimize, None, customer_label="Acme Plumbing Inc.")
    assert path.exists()
    assert path.parent.name == "acme_plumbing_inc"


def _audit_with_security() -> AuditResult:
    """A five-issue audit that also carries a passive security result."""
    from backend.seo.models import SecurityFinding

    audit = _five_issue_audit()
    audit.findings["security"] = {
        "grade": "D",
        "probe_requests_used": 4,
        "checks_run": ["security_headers"],
        "findings": [
            SecurityFinding(
                id="missing_content_security_policy", severity="medium",
                category="headers", title="No Content-Security-Policy header",
                evidence="Content-Security-Policy header absent",
                risk="injected scripts run freely",
                remediation="Add a Content-Security-Policy header.",
                client_headline="One hacked plugin away from a customer-data leak",
                client_impact="Your site has no safety net for a bad script.",
            ).model_dump(),
            SecurityFinding(
                id="tls_certificate_valid", severity="info", category="tls",
                title="TLS certificate valid", evidence="expires 2027-01-01",
                risk="", remediation="No action required.",
                client_headline="Your secure-connection certificate is healthy",
                client_impact="Visitors see the padlock and browsers trust you.",
            ).model_dump(),
        ],
    }
    return audit


def test_write_seo_report_also_writes_technical_security_report(tmp_path, monkeypatch):
    """When a security audit ran, security_audit_technical.md lands next to
    seo_report.md, and keeps the technical detail the client report drops."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    audit = _audit_with_security()
    optimize = _matching_optimize(audit)
    path = write_seo_report(audit, optimize, None)

    technical = path.parent / "security_audit_technical.md"
    assert technical.exists(), "security_audit_technical.md was not written"
    assert technical.stat().st_size > 0

    tech_text = technical.read_text(encoding="utf-8")
    # Technical report keeps the jargon-heavy detail.
    assert "Technical Report" in tech_text
    assert "`missing_content_security_policy`" in tech_text
    assert "Content-Security-Policy header absent" in tech_text
    assert "How to fix:" in tech_text

    # The customer report has the client copy but NOT the technical detail.
    client_text = path.read_text(encoding="utf-8")
    assert "One hacked plugin away from a customer-data leak" in client_text
    assert "missing_content_security_policy" not in client_text
    assert "Content-Security-Policy" not in client_text


def test_write_seo_report_skips_technical_when_no_security_audit(tmp_path, monkeypatch):
    """No security audit -> no companion technical file is written."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    audit = _five_issue_audit()  # no findings["security"]
    optimize = _matching_optimize(audit)
    path = write_seo_report(audit, optimize, None)
    assert not (path.parent / "security_audit_technical.md").exists()


# ---------------------------------------------------------------------------
# Service-level / pipeline test
# ---------------------------------------------------------------------------

def test_audit_and_report_full_pipeline(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    llm_payload = {
        "title": "Plumbing You Can Trust",
        "meta_description": "Fast, transparent, local plumbing services with a written quote and same-day response.",
        "h1": "Plumbing You Can Trust",
        "body_intro": "We deliver fast plumbing service. Local. Honest.",
        "body_main": "We do plumbing. " * 30,
        "cta": "Call us today.",
    }
    _patch_anthropic(monkeypatch, text_body=json.dumps(llm_payload))

    class _StubSettings:
        anthropic_api_key = "stub-key"

    import backend.seo.service as svc_mod
    monkeypatch.setattr(svc_mod, "get_settings", lambda: _StubSettings())

    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_and_report

    result = audit_and_report(
        AuditRequest(url="https://e2e.example.com",
                     keywords=["plumbing"], industry="plumbing"),
        target_keywords=["plumbing"],
    )

    for key in ("audit", "optimize", "content", "report_path", "customer_slug"):
        assert key in result, f"missing key {key} in result"
    assert result["customer_slug"] == "e2e_example_com"

    from pathlib import Path
    p = Path(result["report_path"])
    assert p.exists()
    assert p.stat().st_size > 0
    text = p.read_text(encoding="utf-8")
    assert "SEO Audit & Fix Report" in text
    assert "Plumbing You Can Trust" in text  # LLM-generated h1 ends up in drafts
    # Idempotency: a second call returns cached dict.
    result2 = audit_and_report(
        AuditRequest(url="https://e2e.example.com",
                     keywords=["plumbing"], industry="plumbing"),
        target_keywords=["plumbing"],
    )
    assert result2 == result


def test_app_endpoint_audit_and_report(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _StubSettings:
        anthropic_api_key = ""

    import backend.seo.service as svc_mod
    monkeypatch.setattr(svc_mod, "get_settings", lambda: _StubSettings())

    from fastapi.testclient import TestClient
    from backend.seo.app import app
    client = TestClient(app)
    r = client.post("/audit_and_report", json={
        "url": "https://endpoint.example.com",
        "keywords": ["plumbing"],
        "industry": "plumbing",
        "target_keywords": ["plumbing"],
        "tone": "professional",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("audit", "optimize", "content", "report_path", "customer_slug"):
        assert key in body
    assert body["customer_slug"] == "endpoint_example_com"

    from pathlib import Path
    assert Path(body["report_path"]).exists()
