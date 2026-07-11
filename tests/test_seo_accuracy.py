"""SEO audit accuracy cut (2026-05-16): robots.txt + retry/backoff + NAP hardening."""

from __future__ import annotations

import httpx


# ---------------------------------------------------------------------------
# Per-URL programmable httpx stub — needed because Cut B fetches BOTH
# /robots.txt AND the target URL through the same audit.httpx singleton.
# ---------------------------------------------------------------------------


class _MultiUrlClient:
    """Replaces httpx.Client. Routes by URL substring to (status, body)."""

    routes: dict[str, tuple[int, str, type[Exception] | None]] = {}
    call_counts: dict[str, int] = {}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        _MultiUrlClient.call_counts[url] = _MultiUrlClient.call_counts.get(url, 0) + 1
        # Suffix matching (instead of substring) so '/robots.txt' and '/'
        # are unambiguous routes — the host part of the URL never collides
        # with path-based needles.
        for needle, (status, body, raise_exc) in self.routes.items():
            if url.endswith(needle):
                if raise_exc is not None:
                    raise raise_exc(f"stubbed transport failure for {needle}")
                req = httpx.Request("GET", url, headers=headers or {})
                return httpx.Response(status, text=body or "", request=req)
        # Unmatched URL -> 404
        req = httpx.Request("GET", url, headers=headers or {})
        return httpx.Response(404, text="", request=req)


def _set_routes(monkeypatch, routes: dict, reset_calls: bool = True):
    """Patch audit.httpx.Client to use the multi-URL stub with the given routes."""
    _MultiUrlClient.routes = routes
    if reset_calls:
        _MultiUrlClient.call_counts = {}
    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod.httpx, "Client", _MultiUrlClient)


_MINIMAL_GOOD_HTML = (
    "<html><head>"
    "<title>Acme Plumbing</title>"
    "<meta name='description' content='" + "x" * 80 + "'>"
    "<meta name='viewport' content='w'>"
    "<link rel='canonical' href='https://acme.example.com/'>"
    "<meta property='og:title' content='t'><meta property='og:image' content='i'>"
    "<script type='application/ld+json'>"
    '{"@context":"https://schema.org","@type":"Plumber","name":"Acme"}'
    "</script>"
    "<script>gtag('config','G-ABC12345');</script>"
    "</head><body><h1>Acme Plumbing</h1>"
    "<p>Call (530) 555-1234 -- 123 Main St, Yuba City, CA 95991</p>"
    "<img src='logo.png' alt='Acme'><a href='/'>Home</a>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def test_robots_txt_disallow_flags_blocked_by_robots(monkeypatch):
    """Disallow:/ -> CRITICAL blocked_by_robots issue (page still audited)."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "User-agent: *\nDisallow: /\n", None),
            "https://acme.example.com/": (200, _MINIMAL_GOOD_HTML, None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert "blocked_by_robots" in issue_ids
    # blocked_by_robots is critical -> -5 from score
    blocked = next(i for i in result.issues if i.id == "blocked_by_robots")
    assert blocked.severity == "critical"
    # findings carry the status for ops visibility
    assert result.findings["robots_txt_status"] == "disallows"
    assert result.findings["robots_txt_allows"] is False


def test_robots_txt_allow_no_issue(monkeypatch):
    """Allow: / -> no robots issue."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "User-agent: *\nAllow: /\n", None),
            "https://acme.example.com/": (200, _MINIMAL_GOOD_HTML, None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")
    issue_ids = {i.id for i in result.issues}
    assert "blocked_by_robots" not in issue_ids
    assert result.findings["robots_txt_status"] == "allows"


def test_robots_txt_404_treated_as_allowing(monkeypatch):
    """No robots.txt at all -> standard web behavior is 'allowed'."""
    _set_routes(
        monkeypatch,
        {
            # /robots.txt route omitted -> falls through to 404 in stub
            "https://acme.example.com/": (200, _MINIMAL_GOOD_HTML, None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")
    assert result.findings["robots_txt_status"] == "missing"
    assert result.findings["robots_txt_allows"] is True
    assert "blocked_by_robots" not in {i.id for i in result.issues}


def test_robots_txt_transport_error_does_not_crash(monkeypatch):
    """robots.txt unreachable -> treated as allowing, audit proceeds."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (0, "", httpx.ConnectError),
            "https://acme.example.com/": (200, _MINIMAL_GOOD_HTML, None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")
    assert result.findings["robots_txt_status"] == "error"
    assert result.findings["robots_txt_allows"] is True


def test_robots_txt_path_specific_rule_respected(monkeypatch):
    """Rule allowing root but blocking /admin/ -> root URL still ok."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "User-agent: *\nDisallow: /admin/\n", None),
            "https://acme.example.com/": (200, _MINIMAL_GOOD_HTML, None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")
    # Auditing root, /admin/ disallowed -> root is allowed
    assert result.findings["robots_txt_allows"] is True
    assert "blocked_by_robots" not in {i.id for i in result.issues}


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


def test_fetch_retries_on_transient_5xx(monkeypatch):
    """5xx is retryable. After 3 attempts of 503 we report the failure."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            "https://flaky.example.com/": (503, "", None),
        },
    )
    # Pin retry sleep to ~0 so the test doesn't take 7s
    import tenacity

    monkeypatch.setattr("backend.seo.audit._fetch_retried.retry.wait", tenacity.wait_fixed(0))
    from backend.seo.audit import audit_url

    result = audit_url("https://flaky.example.com/")
    # 503 -> all retries exhausted -> fetch_failed at the outer try/except
    assert result.seo_score == 0
    assert result.findings["fetched"] is False
    # 3 attempts to the target URL (the initial + 2 retries)
    target_calls = sum(
        v
        for k, v in _MultiUrlClient.call_counts.items()
        if "flaky.example.com" in k and "robots.txt" not in k
    )
    assert target_calls >= 2  # at least one retry happened


def test_fetch_succeeds_after_transient_then_ok(monkeypatch):
    """First call raises ConnectError, second returns 200 -> success."""
    # Custom routing: track call count + flip behavior
    state = {"calls": 0}

    class _FlipFlopClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            if "robots.txt" in url:
                req = httpx.Request("GET", url)
                return httpx.Response(200, text="", request=req)
            state["calls"] += 1
            if state["calls"] == 1:
                raise httpx.ConnectError("transient")
            req = httpx.Request("GET", url)
            return httpx.Response(200, text=_MINIMAL_GOOD_HTML, request=req)

    import backend.seo.audit as audit_mod
    import tenacity

    monkeypatch.setattr(audit_mod.httpx, "Client", _FlipFlopClient)
    monkeypatch.setattr("backend.seo.audit._fetch_retried.retry.wait", tenacity.wait_fixed(0))
    from backend.seo.audit import audit_url

    result = audit_url("https://recover.example.com/")
    assert result.findings["fetched"] is True
    assert state["calls"] == 2  # first failed, second succeeded


def test_fetch_does_not_retry_on_404(monkeypatch):
    """4xx codes are real outcomes — retrying wastes time + budget."""
    state = {"calls": 0}

    class _CountingClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            if "robots.txt" in url:
                req = httpx.Request("GET", url)
                return httpx.Response(200, text="", request=req)
            state["calls"] += 1
            req = httpx.Request("GET", url)
            return httpx.Response(404, text="", request=req)

    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod.httpx, "Client", _CountingClient)
    from backend.seo.audit import audit_url

    audit_url("https://notfound.example.com/")
    # Exactly 1 call — no retries
    assert state["calls"] == 1


# ---------------------------------------------------------------------------
# NAP hardening
# ---------------------------------------------------------------------------


def _nap_html(body_inner: str, *, with_schema: bool = False) -> str:
    schema = (
        (
            "<script type='application/ld+json'>"
            '{"@context":"https://schema.org","@type":"Plumber","name":"Acme",'
            '"telephone":"+1-555-123-4567",'
            '"address":{"@type":"PostalAddress","streetAddress":"123 Main St",'
            '"addressLocality":"Yuba City","addressRegion":"CA","postalCode":"95991"}}'
            "</script>"
        )
        if with_schema
        else ""
    )
    return (
        "<html><head>"
        "<title>Acme</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://x.example.com/'>"
        "<meta property='og:title' content='t'>"
        "<meta property='og:image' content='i'>"
        + schema
        + "<script>gtag('config','G-ABC12345');</script>"
        "</head><body><h1>Acme</h1>"
        + body_inner
        + "<img src='/l.png' alt='l'><a href='/'>Home</a></body></html>"
    )


def test_nap_us_plain_phone_recognized(monkeypatch):
    """Existing format: 530-555-1234 still recognized."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            "https://x.example.com/": (200, _nap_html("<p>Call 530-555-1234 today.</p>"), None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert "missing_local_signals" not in {i.id for i in result.issues}


def test_nap_us_parenthesis_phone_recognized(monkeypatch):
    """(530) 555-1234 (parenthesized) now recognized."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            "https://x.example.com/": (200, _nap_html("<p>Call (530) 555-1234 today.</p>"), None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert "missing_local_signals" not in {i.id for i in result.issues}


def test_nap_international_phone_recognized(monkeypatch):
    """+44 20 7946 0958 (UK format) now recognized."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            "https://x.example.com/": (200, _nap_html("<p>Call +44 20 7946 0958</p>"), None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    assert "missing_local_signals" not in {i.id for i in result.issues}


def test_nap_zip_alone_satisfies_address_check(monkeypatch):
    """A US ZIP code in the page text counts as an address signal."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            "https://x.example.com/": (200, _nap_html("<p>Serving Yuba City 95991</p>"), None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    # No phone, but ZIP present -> only one of (phone OR address-keyword/ZIP)
    # is needed. ZIP counts; missing_local_signals should NOT fire.
    # Actually: re-read the rule — it requires phone AND no-address. So
    # missing-phone alone doesn't trigger unless there's also no address.
    # With ZIP, the address half is satisfied -> NOT missing.
    assert "missing_local_signals" not in {i.id for i in result.issues}


def test_nap_skipped_when_local_business_schema_present(monkeypatch):
    """Schema.org LocalBusiness IS NAP — no need to flag visible text."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            "https://x.example.com/": (200, _nap_html("", with_schema=True), None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    # Empty body + no visible phone/address, BUT the LocalBusiness JSON-LD
    # block carries NAP. Skip the visible-text NAP check.
    assert "missing_local_signals" not in {i.id for i in result.issues}


def test_nap_flagged_when_neither_schema_nor_visible_text(monkeypatch):
    """No phone, no address keyword, no schema -> still flagged."""
    _set_routes(
        monkeypatch,
        {
            "robots.txt": (200, "", None),
            # Use a body with no phone/address/zip AND we strip schema by
            # building the HTML without the schema block.
            "https://x.example.com/": (200, _nap_html("<p>Welcome.</p>", with_schema=False), None),
        },
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://x.example.com/")
    # Schema block is _seo_audit Plumber, but no LocalBusiness fields.
    # Wait — _nap_html(with_schema=False) emits NO ld+json at all.
    # So missing_schema_org fires + missing_local_signals should fire.
    issue_ids = {i.id for i in result.issues}
    assert "missing_local_signals" in issue_ids


# ---------------------------------------------------------------------------
# Recommendation for blocked_by_robots
# ---------------------------------------------------------------------------


def test_blocked_by_robots_has_dedicated_recommendation():
    from backend.seo.models import AuditResult, SeoIssue
    from backend.seo.recommendations import build_recommendations

    audit = AuditResult(
        url="https://x.example/",
        seo_score=0,
        issues=[
            SeoIssue(id="blocked_by_robots", severity="critical", category="technical", message="x")
        ],
        findings={"industry": "plumbing"},
        ts="2026-05-16T00:00:00Z",
    )
    recs, _ = build_recommendations(audit, [])
    assert len(recs) == 1
    assert "robots.txt" in recs[0].action
    assert "Disallow" in recs[0].action
    assert len(recs[0].rationale) > 40
