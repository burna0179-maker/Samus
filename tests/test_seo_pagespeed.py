"""PageSpeed Insights client + audit integration + recommendations (Cut C)."""

from __future__ import annotations

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_psi_response(
    *,
    perf: float | None = 0.85,
    access: float | None = 0.95,
    seo: float | None = 0.90,
    bp: float | None = 0.92,
    lcp_ms: float | None = 2100,
    cls: float | None = 0.05,
    tbt_ms: float | None = 150,
    fcp_ms: float | None = 1800,
    si_ms: float | None = 2300,
) -> dict:
    """Build a PSI v5 response dict mirroring the real shape (subset).

    Lighthouse stores category scores as 0.0-1.0 floats and audits as
    numeric values in their natural units (ms for time-based, unitless
    for CLS).
    """
    audits: dict[str, dict] = {}

    def _add(audit_id, value):
        if value is not None:
            audits[audit_id] = {"numericValue": value}

    _add("largest-contentful-paint", lcp_ms)
    _add("cumulative-layout-shift", cls)
    _add("total-blocking-time", tbt_ms)
    _add("first-contentful-paint", fcp_ms)
    _add("speed-index", si_ms)

    def _cat(score):
        return {"score": score} if score is not None else None

    return {
        "lighthouseResult": {
            "categories": {
                "performance": _cat(perf),
                "accessibility": _cat(access),
                "seo": _cat(seo),
                "best-practices": _cat(bp),
            },
            "audits": audits,
        },
    }


class _FakeHttpx:
    """Per-module httpx stub. Falls through to real httpx for exceptions
    + Request/Response constructors, but overrides Client. Lets audit's
    httpx + pagespeed_client's httpx be patched independently — without
    this each setattr on the real httpx singleton would clobber the prior."""

    def __init__(self, client_cls):
        self.Client = client_cls

    def __getattr__(self, name):
        return getattr(httpx, name)


class _PsiStubClient:
    """httpx.Client stub for the PageSpeed module — programmable by env."""

    response_body: dict | str = {}
    status_code: int = 200
    raise_exc: Exception | None = None
    last_params: dict | None = None

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        _PsiStubClient.last_params = params
        if _PsiStubClient.raise_exc is not None:
            raise _PsiStubClient.raise_exc
        req = httpx.Request("GET", url, params=params, headers=headers or {})
        if isinstance(_PsiStubClient.response_body, dict):
            import json

            text = json.dumps(_PsiStubClient.response_body)
        else:
            text = _PsiStubClient.response_body or ""
        return httpx.Response(_PsiStubClient.status_code, text=text, request=req)


@pytest.fixture
def _patch_psi(monkeypatch):
    def _apply(*, body=None, status=200, raise_exc=None):
        _PsiStubClient.response_body = body if body is not None else {}
        _PsiStubClient.status_code = status
        _PsiStubClient.raise_exc = raise_exc
        _PsiStubClient.last_params = None
        import backend.seo.pagespeed_client as ps_mod

        # Patch the MODULE's httpx attribute (not the real httpx.Client),
        # so the audit module's httpx remains untouched. See _FakeHttpx.
        monkeypatch.setattr(ps_mod, "httpx", _FakeHttpx(_PsiStubClient))

    return _apply


# ---------------------------------------------------------------------------
# PageSpeedResult unit tests (client in isolation)
# ---------------------------------------------------------------------------


def test_degraded_when_api_key_unset(monkeypatch):
    """No env var, no kwarg -> result with error='api_key_unset'."""
    monkeypatch.delenv("GOOGLE_PAGESPEED_API_KEY", raising=False)
    from backend.seo.pagespeed_client import audit_pagespeed

    result = audit_pagespeed("https://example.com/")
    assert result.error == "api_key_unset"
    assert result.performance_score is None
    assert result.lcp_ms is None


def test_parses_response_into_scores_and_cwv(_patch_psi):
    _patch_psi(
        body=_fake_psi_response(
            perf=0.42,
            access=0.88,
            seo=0.95,
            bp=0.90,
            lcp_ms=5200,
            cls=0.18,
            tbt_ms=350,
            fcp_ms=2400,
            si_ms=4100,
        )
    )
    from backend.seo.pagespeed_client import audit_pagespeed

    result = audit_pagespeed("https://example.com/", api_key="k_test")
    assert result.error == ""
    assert result.performance_score == 42
    assert result.accessibility_score == 88
    assert result.seo_score == 95
    assert result.best_practices_score == 90
    assert result.lcp_ms == 5200
    assert result.cls == 0.18
    assert result.tbt_ms == 350
    assert result.fcp_ms == 2400
    assert result.speed_index_ms == 4100


def test_requests_all_four_categories(_patch_psi):
    """The PSI call should request all four Lighthouse categories +
    mobile strategy by default — this is what makes the API return all
    the data the issue builder needs."""
    _patch_psi(body=_fake_psi_response())
    from backend.seo.pagespeed_client import audit_pagespeed

    audit_pagespeed("https://example.com/", api_key="k")
    params = _PsiStubClient.last_params
    assert params is not None
    assert params["url"] == "https://example.com/"
    assert params["key"] == "k"
    assert params["strategy"] == "mobile"
    assert set(params["category"]) == {
        "performance",
        "accessibility",
        "seo",
        "best-practices",
    }


def test_missing_category_score_becomes_none(_patch_psi):
    """Lighthouse occasionally skips a category — score=None must pass
    through, not crash."""
    _patch_psi(body=_fake_psi_response(perf=None, access=0.9))
    from backend.seo.pagespeed_client import audit_pagespeed

    result = audit_pagespeed("https://example.com/", api_key="k")
    assert result.performance_score is None
    assert result.accessibility_score == 90


def test_http_error_returns_error_field(_patch_psi):
    _patch_psi(body="quota exceeded", status=429)
    from backend.seo.pagespeed_client import audit_pagespeed

    result = audit_pagespeed("https://example.com/", api_key="k")
    assert "http_429" in result.error
    assert result.performance_score is None


def test_transport_error_returns_error_field(_patch_psi):
    _patch_psi(raise_exc=httpx.ConnectError("upstream unreachable"))
    from backend.seo.pagespeed_client import audit_pagespeed

    result = audit_pagespeed("https://example.com/", api_key="k")
    assert "transport_error" in result.error


def test_invalid_json_returns_error_field(_patch_psi):
    _patch_psi(body="<html>error page</html>", status=200)
    from backend.seo.pagespeed_client import audit_pagespeed

    result = audit_pagespeed("https://example.com/", api_key="k")
    assert "invalid_json" in result.error


# ---------------------------------------------------------------------------
# Audit integration — PSI issues fire when scores cross thresholds
# ---------------------------------------------------------------------------

# Reuse the multi-URL stub pattern from test_seo_accuracy so the audit's
# own httpx (for page fetch + robots.txt) is independent of the PSI httpx.

_MINIMAL_GOOD_HTML = (
    "<html><head>"
    "<title>X</title>"
    "<meta name='description' content='" + "x" * 80 + "'>"
    "<meta name='viewport' content='w'>"
    "<link rel='canonical' href='https://x.example.com/'>"
    "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
    "<script type='application/ld+json'>"
    '{"@context":"https://schema.org","@type":"Plumber","name":"X"}'
    "</script>"
    "<script>gtag('config','G-ABC12345');</script>"
    "</head><body><h1>X</h1>"
    "<p>Call (530) 555-1234 -- 123 Main St, Yuba City 95991</p>"
    "<img src='/l.png' alt='l'><a href='/'>Home</a>"
    "</body></html>"
)


class _AuditPageClient:
    """Stub for audit.httpx — serves robots.txt + page HTML."""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        if url.endswith("/robots.txt"):
            req = httpx.Request("GET", url)
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=req)
        req = httpx.Request("GET", url)
        return httpx.Response(200, text=_MINIMAL_GOOD_HTML, request=req)


def _patch_audit_httpx(monkeypatch):
    import backend.seo.audit as audit_mod

    # Same per-module isolation as _patch_psi — patch the module's httpx
    # attribute, not the real httpx singleton.
    monkeypatch.setattr(audit_mod, "httpx", _FakeHttpx(_AuditPageClient))


def test_audit_no_pagespeed_issue_when_scores_good(monkeypatch, _patch_psi):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=0.95, lcp_ms=1800, cls=0.02))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert not any(i.startswith("pagespeed_") for i in issue_ids)
    # Findings still populated with the scores
    assert result.findings["pagespeed_performance_score"] == 95
    assert result.findings["pagespeed_lcp_ms"] == 1800
    assert result.findings["pagespeed_cls"] == 0.02
    assert result.findings["pagespeed_error"] == ""


def test_audit_flags_pagespeed_performance_poor(monkeypatch, _patch_psi):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=0.42, lcp_ms=2000, cls=0.05))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert "pagespeed_performance_poor" in {i.id for i in result.issues}
    # Cut-off is < 50; 49 -> fires, 50 -> doesn't
    poor = next(i for i in result.issues if i.id == "pagespeed_performance_poor")
    assert poor.severity == "high"


def test_audit_flags_pagespeed_lcp_poor(monkeypatch, _patch_psi):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=0.80, lcp_ms=5500, cls=0.05))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert "pagespeed_lcp_poor" in {i.id for i in result.issues}


def test_audit_flags_pagespeed_cls_poor(monkeypatch, _patch_psi):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=0.80, lcp_ms=2000, cls=0.42))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert "pagespeed_cls_poor" in {i.id for i in result.issues}


def test_audit_no_pagespeed_issues_when_key_unset(monkeypatch):
    """No GOOGLE_PAGESPEED_API_KEY -> PSI module returns error and the
    issue builder no-ops. The audit still ships with on-page findings."""
    _patch_audit_httpx(monkeypatch)
    monkeypatch.delenv("GOOGLE_PAGESPEED_API_KEY", raising=False)
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert not any(i.startswith("pagespeed_") for i in issue_ids)
    # Findings carry the api_key_unset surfaced from the PSI module
    assert result.findings["pagespeed_error"] == "api_key_unset"
    assert result.findings["pagespeed_performance_score"] is None


def test_audit_no_pagespeed_issues_on_psi_transport_error(monkeypatch, _patch_psi):
    """PSI 5xx/transport failure -> error surfaced in findings; no scored
    issues. The audit must not penalize the prospect for a Google outage."""
    _patch_audit_httpx(monkeypatch)
    _patch_psi(raise_exc=httpx.ConnectError("upstream down"))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert not any(i.startswith("pagespeed_") for i in issue_ids)
    assert "transport_error" in result.findings["pagespeed_error"]


# ---------------------------------------------------------------------------
# Recommendations coverage for PageSpeed issue IDs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "issue_id",
    [
        "pagespeed_performance_poor",
        "pagespeed_lcp_poor",
        "pagespeed_cls_poor",
        "pagespeed_performance_needs_improvement",
        "pagespeed_lcp_needs_improvement",
        "pagespeed_cls_needs_improvement",
    ],
)
def test_pagespeed_issue_has_dedicated_recommendation(issue_id):
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations

    audit = AuditResult(
        url="https://x.example/",
        seo_score=0,
        issues=[SeoIssue(id=issue_id, severity="high", category="technical", message="probe")],
        findings={"industry": "plumbing"},
        ts="2026-05-16T00:00:00Z",
    )
    recs, _ = build_recommendations(audit, [])
    assert len(recs) == 1
    # Dedicated rec (not the generic fallback)
    assert not recs[0].action.lower().startswith(f"resolve {issue_id}"), (
        f"{issue_id} fell through to the generic recommendation"
    )
    assert len(recs[0].rationale) > 60


# ---------------------------------------------------------------------------
# Needs-improvement tier (added 2026-05-16 alongside 'poor')
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "perf,expected_id",
    [
        (89, "pagespeed_performance_needs_improvement"),  # top of NI band
        (75, "pagespeed_performance_needs_improvement"),  # middle of NI
        (50, "pagespeed_performance_needs_improvement"),  # bottom of NI
    ],
)
def test_perf_needs_improvement_fires_in_band(monkeypatch, _patch_psi, perf, expected_id):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=perf / 100, lcp_ms=2000, cls=0.05))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert expected_id in issue_ids
    # Severity is medium (NI), not high
    fired = next(i for i in result.issues if i.id == expected_id)
    assert fired.severity == "medium"


@pytest.mark.parametrize("perf", [49, 0])  # below NI band
def test_perf_poor_fires_below_50(monkeypatch, _patch_psi, perf):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=perf / 100, lcp_ms=2000, cls=0.05))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert "pagespeed_performance_poor" in issue_ids
    # NI does NOT also fire — they're mutually exclusive
    assert "pagespeed_performance_needs_improvement" not in issue_ids


@pytest.mark.parametrize("perf", [90, 95, 100])
def test_perf_no_issue_above_89(monkeypatch, _patch_psi, perf):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=perf / 100, lcp_ms=2000, cls=0.05))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert "pagespeed_performance_poor" not in issue_ids
    assert "pagespeed_performance_needs_improvement" not in issue_ids


@pytest.mark.parametrize(
    "lcp,expected_id",
    [
        (4001, "pagespeed_lcp_poor"),
        (4000, "pagespeed_lcp_needs_improvement"),
        (3000, "pagespeed_lcp_needs_improvement"),
        (2501, "pagespeed_lcp_needs_improvement"),
        (2500, None),
        (1800, None),
    ],
)
def test_lcp_tier_boundaries(monkeypatch, _patch_psi, lcp, expected_id):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=0.85, lcp_ms=lcp, cls=0.05))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    lcp_issues = [i for i in issue_ids if i.startswith("pagespeed_lcp_")]
    if expected_id is None:
        assert lcp_issues == []
    else:
        assert lcp_issues == [expected_id], (
            f"expected only {expected_id} at lcp={lcp}, got {lcp_issues}"
        )


@pytest.mark.parametrize(
    "cls,expected_id",
    [
        (0.30, "pagespeed_cls_poor"),
        (0.25, "pagespeed_cls_needs_improvement"),
        (0.15, "pagespeed_cls_needs_improvement"),
        (0.11, "pagespeed_cls_needs_improvement"),
        (0.10, None),
        (0.05, None),
    ],
)
def test_cls_tier_boundaries(monkeypatch, _patch_psi, cls, expected_id):
    _patch_audit_httpx(monkeypatch)
    _patch_psi(body=_fake_psi_response(perf=0.85, lcp_ms=2000, cls=cls))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    issue_ids = {i.id for i in result.issues}
    cls_issues = [i for i in issue_ids if i.startswith("pagespeed_cls_")]
    if expected_id is None:
        assert cls_issues == []
    else:
        assert cls_issues == [expected_id], (
            f"expected only {expected_id} at cls={cls}, got {cls_issues}"
        )


# ---------------------------------------------------------------------------
# Desktop pass — informational findings, no scored issues
# ---------------------------------------------------------------------------


def test_desktop_pass_populates_findings(monkeypatch, _patch_psi):
    """audit_url runs PSI twice (mobile + desktop) when key is set."""
    _patch_audit_httpx(monkeypatch)
    _patch_psi(
        body=_fake_psi_response(
            perf=0.85,
            lcp_ms=2000,
            cls=0.05,
            access=0.92,
            seo=0.95,
            bp=0.88,
            tbt_ms=140,
            fcp_ms=1500,
            si_ms=2200,
        )
    )
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    # Both strategies populated. (Same stub body returned for both calls,
    # so values match — real PSI would differ between mobile + desktop.)
    f = result.findings
    assert f["pagespeed_strategy"] == "mobile"
    assert f["pagespeed_performance_score"] == 85
    assert f["pagespeed_desktop_performance_score"] == 85
    assert f["pagespeed_desktop_accessibility_score"] == 92
    assert f["pagespeed_desktop_seo_score"] == 95
    assert f["pagespeed_desktop_best_practices_score"] == 88
    assert f["pagespeed_desktop_lcp_ms"] == 2000
    assert f["pagespeed_desktop_cls"] == 0.05
    assert f["pagespeed_desktop_tbt_ms"] == 140
    assert f["pagespeed_desktop_fcp_ms"] == 1500
    assert f["pagespeed_desktop_speed_index_ms"] == 2200
    assert f["pagespeed_desktop_error"] == ""


def test_desktop_pass_scores_do_not_drive_issues(monkeypatch, _patch_psi):
    """Desktop scores are informational — even when desktop reports
    catastrophic numbers, no scored issues should fire. Only mobile
    scores drive ranking and so only mobile drives issues."""
    _patch_audit_httpx(monkeypatch)
    # First call (mobile) returns good. Track call count so we can return
    # bad desktop on the second call.
    call_log: list[str] = []

    def _smart_get(_self, url, params=None, headers=None):
        strategy = (params or {}).get("strategy", "mobile")
        call_log.append(strategy)
        body = (
            _fake_psi_response(perf=0.95, lcp_ms=1500, cls=0.02)
            if strategy == "mobile"
            else _fake_psi_response(perf=0.20, lcp_ms=8000, cls=0.50)
        )
        import json

        req = httpx.Request("GET", url, params=params, headers=headers or {})
        return httpx.Response(200, text=json.dumps(body), request=req)

    class _SmartClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        get = _smart_get

    import backend.seo.pagespeed_client as ps_mod

    monkeypatch.setattr(ps_mod, "httpx", _FakeHttpx(_SmartClient))
    monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "k_test")

    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    # Both strategies were called
    assert call_log == ["mobile", "desktop"]
    issue_ids = {i.id for i in result.issues}
    # Mobile was good -> no scored issues
    assert not any(i.startswith("pagespeed_") for i in issue_ids)
    # Desktop scores show in findings but DO NOT generate issues
    assert result.findings["pagespeed_desktop_performance_score"] == 20
    assert result.findings["pagespeed_desktop_lcp_ms"] == 8000
    assert result.findings["pagespeed_desktop_cls"] == 0.5


def test_desktop_pass_skipped_when_api_key_unset(monkeypatch):
    """No env var -> desktop call also returns error, no crash."""
    _patch_audit_httpx(monkeypatch)
    monkeypatch.delenv("GOOGLE_PAGESPEED_API_KEY", raising=False)
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert result.findings["pagespeed_desktop_error"] == "api_key_unset"
    assert result.findings["pagespeed_desktop_performance_score"] is None
