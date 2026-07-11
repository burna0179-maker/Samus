"""Deeper SEO coverage — audit edge cases, recommendations, content, service, app."""

from __future__ import annotations

import httpx


_BAD_HTML = (
    "<!DOCTYPE html><html><head>"
    "<meta name='robots' content='noindex,nofollow'>"
    "</head><body>"
    "<img src='http://insecure.example.com/img.png'>"
    "<script src='http://insecure.example.com/x.js'></script>"
    "</body></html>"
)

_GOOD_HTML = (
    "<!DOCTYPE html><html><head>"
    "<title>Acme Plumbing | Trusted 24/7 Service in Yuba City</title>"
    "<meta name='description' content='Acme Plumbing offers 24/7 emergency plumbing in Yuba City. Licensed, bonded, fair pricing. Call (530) 555-0102 today for a free quote.'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    # Enrichment fixtures (added 2026-05-16) so this 'clean' page passes
    # the new canonical / OG / schema / analytics checks.
    "<link rel='canonical' href='https://acme.example.com/'>"
    "<meta property='og:title' content='Acme Plumbing'>"
    "<meta property='og:image' content='https://acme.example.com/og.jpg'>"
    "<script type='application/ld+json'>"
    '{"@context":"https://schema.org","@type":"Plumber",'
    '"name":"Acme Plumbing"}'
    "</script>"
    "<script>function gtag(){};gtag('config','G-ABC12345');</script>"
    "</head><body><h1>24/7 Emergency Plumbing in Yuba City</h1>"
    "<p>Visit us at 123 Main Street, Yuba City. Call (530) 555-0102.</p>"
    "<img src='/logo.png' alt='Acme Plumbing logo'>"
    "<a href='/services/'>Our services</a>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


class _FakeHttpx:
    """Per-module httpx stub so audit / content patches don't clobber each other.

    Both audit.py and content.py do ``import httpx`` and call ``httpx.Client(...)``.
    monkeypatch.setattr on the real httpx module mutates the singleton, so a
    later patch in the same test wipes out the earlier one. Patching each
    module's ``httpx`` attribute with its own fake keeps them isolated. Any
    attribute not overridden falls through to the real httpx module so
    exception classes (``HTTPError``, ``RequestError`` etc.) keep working in
    ``except`` clauses.
    """

    def __init__(self, client_cls):
        self.Client = client_cls

    def __getattr__(self, name):
        return getattr(httpx, name)


def _patch_audit_fetch(
    monkeypatch, html: str, status: int = 200, raise_exc: Exception | None = None
):
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
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod, "httpx", _FakeHttpx(_Client))


def _patch_anthropic(
    monkeypatch,
    *,
    json_body=None,
    text_body=None,
    raise_exc: Exception | None = None,
    http_status: int = 200,
):
    """Stub content.anthropic_messages with a controllable fake.

    After the migration to ``backend.common.llm_client``, the content
    module no longer imports httpx — the wrapper does. Tests stub the
    wrapper's reference inside the content module so the call shape
    (workcell, prompt, max_tokens) reaches the stub directly and parse-
    failure paths can flip outcome via the wrapper's ``record_outcome``.

    ``raise_exc`` propagates as an ``LlmCallError`` (transport / 5xx).
    ``http_status >= 400`` is rendered the same way for callers that care.
    """
    import backend.seo.content as content_mod
    from backend.common import llm_client

    if text_body is None and json_body is not None:
        # Mirror old contract: a json_body of {} (default empty) means the
        # stub returns no text -> parse should fail with "no text content".
        text_body = ""

    def _fake(*, workcell, api_key, prompt, max_tokens=1024, **kwargs):
        if raise_exc is not None:
            raise llm_client.LlmCallError(f"stub_transport: {raise_exc}")
        if http_status >= 400:
            raise llm_client.LlmCallError(f"anthropic_http_{http_status}: stubbed")
        return (text_body or "", {"input_tokens": 100, "output_tokens": 50})

    monkeypatch.setattr(content_mod, "anthropic_messages", _fake)
    # The wrapper records outcome internally. The content module also calls
    # record_outcome("failure") on parse error — stub that too so tests can
    # observe the call without touching the real budget store.
    flips: list[dict] = []

    def _fake_outcome(workcell, *, outcome, store=None):
        flips.append({"workcell": workcell, "outcome": outcome})

    monkeypatch.setattr(content_mod, "record_outcome", _fake_outcome)
    return flips


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def test_audit_good_page_clean(monkeypatch):
    _patch_audit_fetch(monkeypatch, _GOOD_HTML)
    from backend.seo.audit import audit_url

    result = audit_url(
        "https://acme.example.com", keywords=["plumbing yuba city"], industry="plumbing"
    )
    assert result.seo_score >= 95
    assert len(result.issues) <= 1
    assert result.findings["fetched"] is True


def test_audit_bad_page_flags_multiple_issues(monkeypatch):
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    from backend.seo.audit import audit_url

    result = audit_url("https://bad.example.com")
    issue_ids = {i.id for i in result.issues}
    assert "missing_title" in issue_ids
    assert "missing_meta_description" in issue_ids
    assert "missing_h1" in issue_ids
    assert "missing_viewport_meta" in issue_ids
    assert "mixed_content" in issue_ids
    assert "noindex_directive" in issue_ids
    # 4 high (3 each) + 1 medium (1) + 2 critical (5 each) = 23 -> score 77
    assert result.seo_score < 80


def test_audit_fetch_failure_returns_zero_score(monkeypatch):
    _patch_audit_fetch(monkeypatch, "", raise_exc=httpx.ConnectError("boom"))
    from backend.seo.audit import audit_url

    result = audit_url("https://down.example.com")
    assert result.seo_score == 0
    ids = [i.id for i in result.issues]
    assert ids == ["fetch_failed"]
    assert result.findings["fetched"] is False


def test_score_formula():
    """1 critical (5) + 1 high (3) + 1 medium (1) = 9 -> 100 - 9 = 91."""
    from backend.seo.audit import _score
    from backend.seo.models import SeoIssue

    issues = [
        SeoIssue(id="x1", severity="critical", category="technical", message="m"),
        SeoIssue(id="x2", severity="high", category="content", message="m"),
        SeoIssue(id="x3", severity="medium", category="content", message="m"),
    ]
    assert _score(issues) == 91


def test_audit_uses_regex_when_bs4_unavailable(monkeypatch):
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_HAS_BS4", False)
    result = audit_mod.audit_url("https://bad.example.com")
    ids = {i.id for i in result.issues}
    assert "missing_title" in ids
    assert "missing_h1" in ids
    assert "mixed_content" in ids
    assert "noindex_directive" in ids
    assert result.findings.get("parser") == "regex"


# ---------------------------------------------------------------------------
# recommendations
# ---------------------------------------------------------------------------


def test_every_issue_gets_a_recommendation():
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations

    issues = [
        SeoIssue(id="missing_title", severity="high", category="content", message="m"),
        SeoIssue(id="missing_h1", severity="high", category="content", message="m"),
        SeoIssue(id="mixed_content", severity="critical", category="technical", message="m"),
        SeoIssue(id="missing_local_signals", severity="medium", category="local", message="m"),
    ]
    audit = AuditResult(
        url="https://x.example",
        seo_score=50,
        issues=issues,
        findings={"industry": "plumbing"},
        ts="2026-05-15T00:00:00Z",
    )
    recs, _on_page = build_recommendations(audit, ["foo"])
    assert len(recs) == 4
    priorities = [r.priority for r in recs]
    assert priorities == sorted(priorities, reverse=True)
    assert priorities[0] == 5


def test_priority_maps_from_severity():
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations

    issues = [
        SeoIssue(id="mixed_content", severity="critical", category="technical", message="m"),
        SeoIssue(id="missing_title", severity="high", category="content", message="m"),
        SeoIssue(id="missing_local_signals", severity="medium", category="local", message="m"),
    ]
    audit = AuditResult(
        url="https://x.example",
        seo_score=50,
        issues=issues,
        findings={},
        ts="2026-05-15T00:00:00Z",
    )
    recs, _ = build_recommendations(audit, [])
    by_area = {r.area: r for r in recs}
    assert by_area["technical"].priority == 5
    assert by_area["title"].priority == 4
    assert by_area["local"].priority == 3


def test_on_page_changes_use_primary_keyword():
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations

    audit = AuditResult(
        url="https://x.example",
        seo_score=50,
        issues=[SeoIssue(id="missing_title", severity="high", category="content", message="m")],
        findings={"industry": "plumbing"},
        ts="2026-05-15T00:00:00Z",
    )
    _, on_page = build_recommendations(audit, ["yuba city plumbing"])
    assert "title" in on_page
    assert "Yuba City Plumbing" in on_page["title"]


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------


def test_fallback_no_key_returns_templated():
    from backend.seo.content import generate_content_drafts
    from backend.seo.models import OptimizeResult

    drafts, wc, used, _cost = generate_content_drafts(
        "https://x.example",
        OptimizeResult(
            url="https://x.example",
            recommendations=[],
            on_page_changes={},
            ts="2026-05-15T00:00:00Z",
        ),
        ["plumbing"],
        "professional",
        anthropic_api_key=None,
    )
    assert used is False
    assert wc > 0
    for f in ("title", "meta_description", "h1", "body_intro", "body_main", "cta"):
        assert f in drafts and drafts[f].strip()


def test_mocked_anthropic_success(monkeypatch):
    import json

    payload = {
        "title": "Yuba City Plumbing | Trusted 24/7",
        "meta_description": "Acme Plumbing - fast, transparent, local. Call today for a free quote on any service big or small.",
        "h1": "Plumbing You Can Trust",
        "body_intro": "We deliver fast service. We are local. We are transparent.",
        "body_main": "We do plumbing. We do drains. We do water heaters. " * 8,
        "cta": "Call us today for a free consultation.",
    }
    _patch_anthropic(monkeypatch, text_body=json.dumps(payload))
    from backend.seo.content import generate_content_drafts
    from backend.seo.models import OptimizeResult

    drafts, wc, used, _cost = generate_content_drafts(
        "https://x.example",
        OptimizeResult(
            url="https://x.example",
            recommendations=[],
            on_page_changes={},
            ts="2026-05-15T00:00:00Z",
        ),
        ["plumbing"],
        "professional",
        anthropic_api_key="sk-test-key",
    )
    assert used is True
    assert drafts["h1"] == "Plumbing You Can Trust"
    assert wc > 0


def test_anthropic_transport_error_falls_back(monkeypatch):
    _patch_anthropic(monkeypatch, raise_exc=httpx.ConnectError("network down"))
    from backend.seo.content import generate_content_drafts
    from backend.seo.models import OptimizeResult

    drafts, wc, used, _cost = generate_content_drafts(
        "https://x.example",
        OptimizeResult(
            url="https://x.example",
            recommendations=[],
            on_page_changes={"title": "Existing"},
            ts="2026-05-15T00:00:00Z",
        ),
        ["plumbing"],
        "professional",
        anthropic_api_key="sk-test-key",
    )
    assert used is False
    assert wc > 0
    assert drafts["title"] == "Existing"


def test_anthropic_malformed_json_falls_back(monkeypatch):
    _patch_anthropic(monkeypatch, text_body="not json")
    from backend.seo.content import generate_content_drafts
    from backend.seo.models import OptimizeResult

    drafts, wc, used, _cost = generate_content_drafts(
        "https://x.example",
        OptimizeResult(
            url="https://x.example",
            recommendations=[],
            on_page_changes={},
            ts="2026-05-15T00:00:00Z",
        ),
        ["plumbing"],
        "professional",
        anthropic_api_key="sk-test-key",
    )
    assert used is False
    assert wc > 0


def test_word_count_nonzero():
    from backend.seo.content import _word_count

    drafts = {"title": "hello world", "meta_description": "one two three"}
    assert _word_count(drafts) == 5


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def test_audit_site_idempotent(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _GOOD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    a = audit_site(AuditRequest(url="https://idem.example.com"))
    b = audit_site(AuditRequest(url="https://idem.example.com"))
    assert a.model_dump() == b.model_dump()


def test_optimize_page_idempotent(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.seo.models import AuditRequest, OptimizeRequest
    from backend.seo.service import audit_site, optimize_page

    audit = audit_site(AuditRequest(url="https://opt.example.com"))
    a = optimize_page(OptimizeRequest(url=audit.url, audit_data=audit, target_keywords=["foo"]))
    b = optimize_page(OptimizeRequest(url=audit.url, audit_data=audit, target_keywords=["foo"]))
    assert a.model_dump() == b.model_dump()


def test_generate_content_uses_settings_anthropic_key(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    import json

    payload = {
        "title": "T",
        "meta_description": "M" * 50,
        "h1": "H",
        "body_intro": "intro",
        "body_main": "main " * 50,
        "cta": "call now",
    }
    _patch_anthropic(monkeypatch, text_body=json.dumps(payload))

    class _StubSettings:
        anthropic_api_key = "stub-key"

    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: _StubSettings())

    from backend.seo.models import ContentRequest, OptimizeResult
    from backend.seo.service import generate_content

    # 2+ keywords + 1+ on_page_change satisfies generate_content's top-N
    # gate (Lever 2.2 / project_samus_llm_token_policy).
    result = generate_content(
        ContentRequest(
            url="https://gen.example.com",
            optimization_data=OptimizeResult(
                url="https://gen.example.com",
                recommendations=[],
                on_page_changes={"title": "Plumbing | Reliable Local Experts"},
                ts="2026-05-15T00:00:00Z",
            ),
            target_keywords=["plumbing", "emergency plumber"],
            tone="professional",
        )
    )
    assert result.used_llm is True
    assert result.page_drafts["title"] == "T"


def test_full_pipeline_end_to_end(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    import json

    llm_payload = {
        "title": "Plumbing You Can Trust",
        "meta_description": "Fast, transparent, local plumbing services with a written quote and a one-business-day response.",
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

    from backend.seo.models import (
        AuditRequest,
        ContentRequest,
        OptimizeRequest,
    )
    from backend.seo.service import audit_site, generate_content, optimize_page

    audit = audit_site(AuditRequest(url="https://e2e.example.com"))
    assert audit.seo_score < 100
    opt = optimize_page(
        OptimizeRequest(url=audit.url, audit_data=audit, target_keywords=["plumbing"])
    )
    assert len(opt.recommendations) >= 1
    # 2+ keywords needed for generate_content's top-N gate; opt.on_page_changes
    # is already populated by optimize_page (see Lever 2.2).
    content = generate_content(
        ContentRequest(
            url=opt.url,
            optimization_data=opt,
            target_keywords=["plumbing", "emergency plumber"],
            tone="professional",
        )
    )
    assert content.used_llm is True
    assert content.word_count > 0
    assert content.page_drafts["title"] == "Plumbing You Can Trust"


# ---------------------------------------------------------------------------
# app endpoints (TestClient)
# ---------------------------------------------------------------------------


def test_audit_endpoint_returns_audit_result(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _GOOD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)
    r = client.post(
        "/audit",
        json={
            "url": "https://acme.example.com",
            "keywords": ["plumbing"],
            "industry": "plumbing",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "seo_score" in body
    assert body["url"] == "https://acme.example.com"


def test_optimize_endpoint_returns_optimize_result(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)
    audit_r = client.post(
        "/audit",
        json={
            "url": "https://bad.example.com",
            "keywords": [],
            "industry": "",
        },
    )
    audit = audit_r.json()
    r = client.post(
        "/optimize",
        json={
            "url": audit["url"],
            "audit_data": audit,
            "target_keywords": ["plumbing"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["recommendations"], list)
    assert len(body["recommendations"]) >= 1


def test_generate_endpoint_fallback_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _StubSettings:
        anthropic_api_key = ""

    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: _StubSettings())

    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)
    r = client.post(
        "/generate",
        json={
            "url": "https://gen.example.com",
            "optimization_data": {
                "url": "https://gen.example.com",
                "recommendations": [],
                "on_page_changes": {},
                "ts": "2026-05-15T00:00:00Z",
            },
            "target_keywords": ["plumbing"],
            "tone": "professional",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["used_llm"] is False
    for f in ("title", "meta_description", "h1", "body_intro", "body_main", "cta"):
        assert f in body["page_drafts"] and body["page_drafts"][f].strip()


def test_work_routes_by_metadata_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _GOOD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-work",
            "payload": {
                "url": "https://work.example.com",
                "keywords": ["plumbing"],
                "industry": "plumbing",
            },
            "metadata": {"action": "audit_site"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "seo_score" in body
    assert body["url"] == "https://work.example.com"


def test_work_routes_optimize_page_action(tmp_path, monkeypatch):
    """The /work dispatcher routes metadata.action='optimize_page' to
    service.optimize_page — the SEO pipeline's stage-2 capability."""
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)

    audit = client.post(
        "/work",
        json={
            "task_id": "t-audit",
            "payload": {"url": "https://opt.example.com", "keywords": [], "industry": ""},
            "metadata": {"action": "audit_site"},
        },
    ).json()

    r = client.post(
        "/work",
        json={
            "task_id": "t-opt",
            "payload": {
                "url": audit["url"],
                "audit_data": audit,
                "target_keywords": ["plumbing"],
            },
            "metadata": {"action": "optimize_page"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["recommendations"], list)
    assert len(body["recommendations"]) >= 1
    assert body["url"] == "https://opt.example.com"


def test_work_routes_generate_content_action(tmp_path, monkeypatch):
    """The /work dispatcher routes metadata.action='generate_content' to
    service.generate_content — the SEO pipeline's stage-3 capability."""
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _StubSettings:
        anthropic_api_key = ""

    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: _StubSettings())

    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-gen",
            "payload": {
                "url": "https://gen.example.com",
                "optimization_data": {
                    "url": "https://gen.example.com",
                    "recommendations": [],
                    "on_page_changes": {},
                    "ts": "2026-05-15T00:00:00Z",
                },
                "target_keywords": ["plumbing"],
                "tone": "professional",
            },
            "metadata": {"action": "generate_content"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["used_llm"] is False
    for f in ("title", "meta_description", "h1", "body_intro", "body_main", "cta"):
        assert f in body["page_drafts"] and body["page_drafts"][f].strip()


def test_work_rejects_unknown_action(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.seo.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-bad",
            "payload": {"url": "https://x.example"},
            "metadata": {"action": "not_a_real_action"},
        },
    )
    assert r.status_code == 400
    assert "unknown_action" in r.json()["detail"]


# ---------------------------------------------------------------------------
# SeoWorker SQS action routing (mirrors the /work dispatcher)
# ---------------------------------------------------------------------------


class _Envelope:
    """Minimal stand-in for the worker envelope: .action + .payload."""

    def __init__(self, action, payload):
        self.action = action
        self.payload = payload


def _seo_worker():
    """Construct a SeoWorker with a stub runtime, or skip if worker_base
    is unavailable in this environment."""
    import pytest

    from backend.seo.worker import SeoWorker, _IMPORT_ERROR

    if _IMPORT_ERROR is not None:
        pytest.skip(f"worker_base unavailable: {_IMPORT_ERROR!r}")
    return SeoWorker.__new__(SeoWorker)  # bypass runtime wiring; handle() is pure


def test_worker_routes_audit_site(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _GOOD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    worker = _seo_worker()
    out = worker.handle(
        _Envelope(
            "audit_site",
            {
                "url": "https://w-audit.example.com",
                "keywords": [],
                "industry": "",
            },
        )
    )
    assert out["url"] == "https://w-audit.example.com"
    assert "seo_score" in out


def test_worker_routes_optimize_page(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _patch_audit_fetch(monkeypatch, _BAD_HTML)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    worker = _seo_worker()
    audit = worker.handle(
        _Envelope(
            "audit_site",
            {
                "url": "https://w-opt.example.com",
                "keywords": [],
                "industry": "",
            },
        )
    )
    out = worker.handle(
        _Envelope(
            "optimize_page",
            {
                "url": audit["url"],
                "audit_data": audit,
                "target_keywords": ["plumbing"],
            },
        )
    )
    assert isinstance(out["recommendations"], list)
    assert len(out["recommendations"]) >= 1


def test_worker_routes_generate_content(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SEO_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    class _StubSettings:
        anthropic_api_key = ""

    import backend.seo.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: _StubSettings())

    worker = _seo_worker()
    out = worker.handle(
        _Envelope(
            "generate_content",
            {
                "url": "https://w-gen.example.com",
                "optimization_data": {
                    "url": "https://w-gen.example.com",
                    "recommendations": [],
                    "on_page_changes": {},
                    "ts": "2026-05-15T00:00:00Z",
                },
                "target_keywords": ["plumbing"],
                "tone": "professional",
            },
        )
    )
    assert out["used_llm"] is False
    assert out["word_count"] > 0


def test_worker_rejects_unknown_action():
    import pytest

    worker = _seo_worker()
    with pytest.raises(ValueError, match="unknown_action"):
        worker.handle(_Envelope("not_a_real_action", {}))
