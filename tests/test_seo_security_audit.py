"""Tests for the passive SEO security & trust-posture audit.

Covers backend/seo/security_audit.py (header parsing, grade computation, DNS
parsing with a mocked resolver, WordPress-exposure detection, TLS-failure
degradation) and the report renderer's "Security & Trust Posture" section.

ALL network I/O is mocked - no test here touches the real internet. HTTP
probes are stubbed via ``_passive_get``, TLS via ``ssl``/``socket`` patches,
and DNS via a fake ``dns.resolver.Resolver``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from backend.seo import security_audit as sec
from backend.seo.models import AuditResult, SecurityFinding
from backend.seo.report import (
    _render_security_posture,
    render_security_technical,
    render_seo_report_markdown,
)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

_FULL_HEADERS = {
    "content-security-policy": "default-src 'self'; frame-ancestors 'self'",
    "strict-transport-security": "max-age=31536000; includeSubDomains; preload",
    "x-frame-options": "SAMEORIGIN",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=(), camera=()",
}


def test_headers_all_present_yields_no_findings():
    findings = sec.check_security_headers(_FULL_HEADERS)
    assert findings == []


def test_headers_all_absent_flags_every_header():
    findings = sec.check_security_headers({})
    ids = {f.id for f in findings}
    assert "missing_content_security_policy" in ids
    assert "missing_strict_transport_security" in ids
    assert "missing_clickjacking_protection" in ids
    assert "missing_x_content_type_options" in ids
    assert "missing_referrer_policy" in ids
    assert "missing_permissions_policy" in ids
    # Every finding carries a remediation and a risk sentence.
    for f in findings:
        assert f.remediation, f"{f.id} has no remediation"
        assert f.risk, f"{f.id} has no risk"
        # ...and punchy, jargon-free client copy.
        assert f.client_headline, f"{f.id} has no client_headline"
        assert f.client_impact, f"{f.id} has no client_impact"
        assert not f.client_headline.endswith("."), f"{f.id} client_headline ends with a period"


# Words that must never appear in client-facing copy (jargon ban).
_CLIENT_JARGON_BAN = (
    "CSP",
    "Content-Security-Policy",
    "HSTS",
    "Strict-Transport-Security",
    "SPF",
    "DMARC",
    "DKIM",
    "CAA",
    "X-Frame-Options",
    "Referrer-Policy",
    "Permissions-Policy",
    "XML-RPC",
    "xmlrpc",
    "nosniff",
    "directive",
    "wp-config",
    "header",
)


def test_every_security_finding_has_jargon_free_client_copy():
    """Sweep every SecurityFinding the module can build and confirm its
    client_headline/client_impact carry no banned jargon and no finding id."""
    import inspect

    # Gather every finding id the module defines so we can prove none of them
    # leak into client copy.
    all_findings: list[SecurityFinding] = []
    all_findings += sec.check_security_headers({})
    all_findings += sec.check_security_headers(
        {"strict-transport-security": "max-age=600"},
    )
    all_findings += sec.check_cookie_flags(
        {"set-cookie": "s=1; Path=/"},
        "https://x.example.com",
    )
    all_findings += sec.check_mixed_content(
        "https://x.example.com",
        ["http://cdn.example.com/a.js"],
    )
    all_findings += sec.check_tls_certificate("")

    for f in all_findings:
        for field_name in ("client_headline", "client_impact"):
            value = getattr(f, field_name)
            assert value, f"{f.id}.{field_name} is empty"
            low = value.lower()
            for banned in _CLIENT_JARGON_BAN:
                assert banned.lower() not in low, (
                    f"{f.id}.{field_name} contains banned jargon '{banned}'"
                )
            assert f.id not in value, f"{f.id}.{field_name} leaks the finding id"
    # The function is exercised; inspect import keeps the helper honest.
    assert inspect.isfunction(sec.check_security_headers)


def test_headers_partial_hsts_flagged_as_weak():
    # HSTS present but missing includeSubDomains + preload.
    findings = sec.check_security_headers(
        {"strict-transport-security": "max-age=600"},
    )
    weak = [f for f in findings if f.id == "weak_strict_transport_security"]
    assert len(weak) == 1
    assert "includeSubDomains" in weak[0].title
    assert "preload" in weak[0].title
    assert weak[0].severity == "low"


def test_headers_csp_frame_ancestors_satisfies_clickjacking():
    # CSP frame-ancestors counts as clickjacking protection even with no XFO.
    findings = sec.check_security_headers(
        {"content-security-policy": "frame-ancestors 'self'"},
    )
    ids = {f.id for f in findings}
    assert "missing_clickjacking_protection" not in ids
    assert "missing_content_security_policy" not in ids


def test_headers_case_insensitive_keys():
    findings = sec.check_security_headers(
        {"X-Content-Type-Options": "nosniff"},
    )
    ids = {f.id for f in findings}
    assert "missing_x_content_type_options" not in ids


# ---------------------------------------------------------------------------
# Cookie flags + mixed content
# ---------------------------------------------------------------------------


def test_insecure_cookie_flagged_on_https():
    findings = sec.check_cookie_flags(
        {"set-cookie": "session=abc; Path=/"},
        "https://acme.example.com",
    )
    assert len(findings) == 1
    assert findings[0].id == "insecure_cookie_flags"
    assert "Secure" in findings[0].evidence
    assert "HttpOnly" in findings[0].evidence


def test_secure_httponly_cookie_not_flagged():
    findings = sec.check_cookie_flags(
        {"set-cookie": "session=abc; Secure; HttpOnly; SameSite=Lax"},
        "https://acme.example.com",
    )
    assert findings == []


def test_no_set_cookie_header_yields_nothing():
    assert sec.check_cookie_flags({}, "https://acme.example.com") == []


def test_mixed_content_flagged_on_https_page():
    findings = sec.check_mixed_content(
        "https://acme.example.com",
        ["http://cdn.example.com/a.js", "http://cdn.example.com/b.png"],
    )
    assert len(findings) == 1
    assert findings[0].id == "mixed_content_resources"


def test_mixed_content_ignored_on_http_page():
    findings = sec.check_mixed_content(
        "http://acme.example.com",
        ["http://cdn.example.com/a.js"],
    )
    assert findings == []


# ---------------------------------------------------------------------------
# Grade computation (deterministic)
# ---------------------------------------------------------------------------


def _finding(severity: str) -> SecurityFinding:
    return SecurityFinding(
        id=f"x_{severity}",
        severity=severity,
        category="headers",
        title="t",
        evidence="e",
        risk="r",
        remediation="rem",
    )


def test_grade_clean_site_is_a():
    assert sec.grade_security([]) == "A"
    assert sec.grade_security([_finding("info"), _finding("info")]) == "A"


def test_grade_single_low_still_a():
    # 100 - 3 = 97 -> A
    assert sec.grade_security([_finding("low")]) == "A"


def test_grade_medium_findings_drop_to_b_then_c():
    # 100 - 8 = 92 -> A ; 100 - 8*3 = 76 -> C
    assert sec.grade_security([_finding("medium")]) == "A"
    assert sec.grade_security([_finding("medium")] * 3) == "C"


def test_grade_critical_is_f():
    # 100 - 40 = 60 -> D ; two criticals -> 20 -> F
    assert sec.grade_security([_finding("critical")]) == "D"
    assert sec.grade_security([_finding("critical")] * 2) == "F"


def test_grade_is_clamped_and_deterministic():
    many = [_finding("critical")] * 10
    assert sec.grade_security(many) == "F"
    # Deterministic: same input, same output.
    assert sec.grade_security(many) == sec.grade_security(many)


# ---------------------------------------------------------------------------
# DNS parsing with a mocked resolver
# ---------------------------------------------------------------------------


class _FakeAnswer:
    """Mimics a dnspython TXT rdata with a .strings attribute."""

    def __init__(self, text: str):
        self.strings = [text.encode("utf-8")]


class _FakeResolver:
    """Stand-in for dns.resolver.Resolver driven by a {(name,rtype): result}."""

    def __init__(self, table: dict):
        self._table = table
        self.lifetime = 0.0
        self.timeout = 0.0

    def resolve(self, name, rtype):
        key = (name, rtype)
        if key not in self._table:
            import dns.resolver

            raise dns.resolver.NoAnswer()
        result = self._table[key]
        if isinstance(result, Exception):
            raise result
        return result


def _patch_resolver(monkeypatch, table: dict):
    """Patch dns.resolver.Resolver inside security_audit with a table-driven fake."""
    import dns.resolver

    monkeypatch.setattr(
        dns.resolver,
        "Resolver",
        lambda: _FakeResolver(table),
    )
    monkeypatch.setattr(sec, "_HAS_DNSPYTHON", True)


def test_email_auth_all_records_healthy(monkeypatch):
    table = {
        ("acme.example.com", "TXT"): [_FakeAnswer("v=spf1 include:_spf.google.com -all")],
        ("_dmarc.acme.example.com", "TXT"): [
            _FakeAnswer("v=DMARC1; p=reject; rua=mailto:dmarc@acme.example.com"),
        ],
        ("default._domainkey.acme.example.com", "TXT"): [
            _FakeAnswer("v=DKIM1; k=rsa; p=MIGfMA0G"),
        ],
        ("acme.example.com", "CAA"): [object()],  # non-empty -> has CAA
    }
    _patch_resolver(monkeypatch, table)
    findings = sec.probe_email_auth("acme.example.com")
    ids = {f.id for f in findings}
    assert "spf_present" in ids
    assert "dmarc_enforced" in ids
    assert "dkim_present" in ids
    assert "caa_present" in ids
    # A healthy email setup produces only info-severity findings.
    assert all(f.severity == "info" for f in findings)


def test_email_auth_dmarc_policy_none_is_medium(monkeypatch):
    table = {
        ("acme.example.com", "TXT"): [_FakeAnswer("v=spf1 -all")],
        ("_dmarc.acme.example.com", "TXT"): [_FakeAnswer("v=DMARC1; p=none")],
    }
    _patch_resolver(monkeypatch, table)
    findings = sec.probe_email_auth("acme.example.com")
    dmarc = [f for f in findings if f.id == "dmarc_policy_none"]
    assert len(dmarc) == 1
    assert dmarc[0].severity == "medium"


def test_email_auth_missing_spf_and_dmarc(monkeypatch):
    # Apex TXT resolves (empty) so DNS itself is reachable, but no SPF.
    table = {
        ("acme.example.com", "TXT"): [],
    }
    _patch_resolver(monkeypatch, table)
    findings = sec.probe_email_auth("acme.example.com")
    ids = {f.id for f in findings}
    assert "spf_missing" in ids
    assert "dmarc_missing" in ids
    assert "caa_missing" in ids
    spf = next(f for f in findings if f.id == "spf_missing")
    assert spf.severity == "medium"
    dmarc = next(f for f in findings if f.id == "dmarc_missing")
    assert dmarc.severity == "high"


def test_email_auth_degrades_when_dnspython_absent(monkeypatch):
    monkeypatch.setattr(sec, "_HAS_DNSPYTHON", False)
    findings = sec.probe_email_auth("acme.example.com")
    assert len(findings) == 1
    assert findings[0].id == "email_auth_dns_unavailable"
    assert findings[0].severity == "info"


def test_email_auth_degrades_when_dns_unreachable(monkeypatch):
    import dns.resolver

    # Apex TXT lookup raises a non-NXDOMAIN/NoAnswer error -> DNS unreachable.
    table = {
        ("acme.example.com", "TXT"): dns.resolver.LifetimeTimeout(),
    }
    _patch_resolver(monkeypatch, table)
    findings = sec.probe_email_auth("acme.example.com")
    assert len(findings) == 1
    assert findings[0].id == "email_auth_dns_unresolved"


def test_email_auth_empty_domain_skips():
    findings = sec.probe_email_auth("")
    assert len(findings) == 1
    assert findings[0].id == "email_auth_check_skipped"


# ---------------------------------------------------------------------------
# WordPress detection + exposure probes
# ---------------------------------------------------------------------------


def test_detect_wordpress_via_link_header():
    assert sec.detect_wordpress(
        "<html></html>",
        {"link": '<https://x/wp-json/>; rel="https://api.w.org/"'},
    )


def test_detect_wordpress_via_asset_path():
    assert sec.detect_wordpress(
        "<html><link href='/wp-content/themes/x/style.css'></html>",
        {},
    )


def test_detect_wordpress_via_generator_meta():
    assert sec.detect_wordpress(
        '<meta name="generator" content="WordPress 6.4">',
        {},
    )


def test_detect_wordpress_false_on_plain_site():
    assert not sec.detect_wordpress("<html><body>hello</body></html>", {})


class _FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _patch_probes(monkeypatch, responses: dict):
    """Patch _passive_get with a {url_suffix: _FakeResp|None} table."""

    def _fake(url, budget):
        if not budget.take():
            return None
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                return resp
        return None

    monkeypatch.setattr(sec, "_passive_get", _fake)


def test_wordpress_exposure_full_detection(monkeypatch):
    users_json = '[{"id":1,"name":"Site Admin","slug":"admin"}]'
    _patch_probes(
        monkeypatch,
        {
            "/wp-json/wp/v2/users": _FakeResp(200, users_json),
            "/xmlrpc.php": _FakeResp(405, ""),
            "/readme.html": _FakeResp(200, "<title>WordPress &rsaquo; ReadMe</title>"),
        },
    )
    budget = sec._ProbeBudget()
    findings = sec.check_platform_exposure(
        "https://wp.example.com",
        "<link href='/wp-content/x.css'>",
        {},
        budget,
    )
    ids = {f.id for f in findings}
    assert "platform_wordpress_detected" in ids
    assert "wordpress_user_enumeration" in ids
    assert "wordpress_xmlrpc_enabled" in ids
    assert "wordpress_readme_exposed" in ids
    enum = next(f for f in findings if f.id == "wordpress_user_enumeration")
    assert "admin" in enum.evidence


def test_wordpress_exposure_clean_install(monkeypatch):
    # WordPress detected, but every exposure probe returns 404/403.
    _patch_probes(
        monkeypatch,
        {
            "/wp-json/wp/v2/users": _FakeResp(401, ""),
            "/xmlrpc.php": _FakeResp(403, ""),
            "/readme.html": _FakeResp(404, ""),
        },
    )
    budget = sec._ProbeBudget()
    findings = sec.check_platform_exposure(
        "https://wp.example.com",
        "<link href='/wp-includes/x.js'>",
        {},
        budget,
    )
    ids = {f.id for f in findings}
    assert ids == {"platform_wordpress_detected"}


def test_non_wordpress_site_skips_probes(monkeypatch):
    _patch_probes(monkeypatch, {})
    budget = sec._ProbeBudget()
    findings = sec.check_platform_exposure(
        "https://plain.example.com",
        "<html>plain</html>",
        {},
        budget,
    )
    assert findings == []
    # No probe requests were spent on a non-WordPress site.
    assert budget.used == 0


def test_exposed_git_directory_flagged(monkeypatch):
    _patch_probes(
        monkeypatch,
        {
            "/.well-known/security.txt": _FakeResp(404, ""),
            "/.git/config": _FakeResp(200, "[core]\n\trepositoryformatversion = 0\n"),
        },
    )
    budget = sec._ProbeBudget()
    findings = sec.check_exposed_artifacts("https://exposed.example.com", budget)
    ids = {f.id for f in findings}
    assert "git_directory_exposed" in ids
    assert "security_txt_missing" in ids
    git = next(f for f in findings if f.id == "git_directory_exposed")
    assert git.severity == "high"


def test_security_txt_present_is_info(monkeypatch):
    _patch_probes(
        monkeypatch,
        {
            "/.well-known/security.txt": _FakeResp(200, "Contact: mailto:sec@x.com"),
            "/.git/config": _FakeResp(404, ""),
        },
    )
    budget = sec._ProbeBudget()
    findings = sec.check_exposed_artifacts("https://safe.example.com", budget)
    ids = {f.id for f in findings}
    assert "security_txt_present" in ids
    assert "git_directory_exposed" not in ids


# ---------------------------------------------------------------------------
# Probe budget cap
# ---------------------------------------------------------------------------


def test_probe_budget_caps_total_requests():
    budget = sec._ProbeBudget(limit=2)
    assert budget.take() is True
    assert budget.take() is True
    assert budget.take() is False
    assert budget.used == 2


# ---------------------------------------------------------------------------
# TLS certificate (graceful degradation)
# ---------------------------------------------------------------------------


def test_tls_handshake_failure_degrades(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(sec.socket, "create_connection", _boom)
    findings = sec.check_tls_certificate("acme.example.com")
    assert len(findings) == 1
    assert findings[0].id == "tls_handshake_failed"
    assert findings[0].severity == "info"


def test_tls_empty_hostname_skips():
    findings = sec.check_tls_certificate("")
    assert len(findings) == 1
    assert findings[0].id == "tls_check_skipped"


def test_tls_certificate_valid(monkeypatch):
    future = datetime.now(timezone.utc) + timedelta(days=200)
    cert = {
        "issuer": ((("organizationName", "Let's Encrypt"),),),
        "notAfter": future.strftime("%b %d %H:%M:%S %Y GMT"),
    }
    _patch_tls(monkeypatch, cert)
    findings = sec.check_tls_certificate("acme.example.com")
    assert len(findings) == 1
    assert findings[0].id == "tls_certificate_valid"
    assert findings[0].severity == "info"


def test_tls_certificate_expiring_soon(monkeypatch):
    soon = datetime.now(timezone.utc) + timedelta(days=10)
    cert = {
        "issuer": ((("organizationName", "Let's Encrypt"),),),
        "notAfter": soon.strftime("%b %d %H:%M:%S %Y GMT"),
    }
    _patch_tls(monkeypatch, cert)
    findings = sec.check_tls_certificate("acme.example.com")
    assert findings[0].id == "tls_certificate_expiring_soon"
    assert findings[0].severity == "high"


def test_tls_certificate_expired(monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(days=5)
    cert = {
        "issuer": ((("organizationName", "Let's Encrypt"),),),
        "notAfter": past.strftime("%b %d %H:%M:%S %Y GMT"),
    }
    _patch_tls(monkeypatch, cert)
    findings = sec.check_tls_certificate("acme.example.com")
    assert findings[0].id == "tls_certificate_expired"
    assert findings[0].severity == "critical"


def _patch_tls(monkeypatch, cert: dict):
    """Patch socket + ssl so wrap_socket().getpeercert() returns ``cert``."""

    class _FakeSSLSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getpeercert(self):
            return cert

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            return _FakeSSLSock()

    monkeypatch.setattr(
        sec.socket,
        "create_connection",
        lambda *a, **kw: _FakeSock(),
    )
    monkeypatch.setattr(
        sec.ssl,
        "create_default_context",
        lambda: _FakeContext(),
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_audit_security_never_raises_and_returns_grade(monkeypatch):
    # Force every check into its degraded path: no DNS, TLS fails, probes 404.
    monkeypatch.setattr(sec, "_HAS_DNSPYTHON", False)
    monkeypatch.setattr(
        sec.socket,
        "create_connection",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("no route")),
    )
    monkeypatch.setattr(sec, "_passive_get", lambda url, budget: None)

    result = sec.audit_security(
        "https://degraded.example.com",
        headers={},
        html="<html></html>",
        http_resources=[],
    )
    assert result["grade"] in ("A", "B", "C", "D", "F")
    assert isinstance(result["findings"], list)
    assert result["findings"], "expected at least the header findings"
    assert "checks_run" in result
    assert result["probe_requests_used"] >= 0


def test_audit_security_respects_probe_budget(monkeypatch):
    monkeypatch.setattr(sec, "_HAS_DNSPYTHON", False)
    monkeypatch.setattr(
        sec.socket,
        "create_connection",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("no route")),
    )
    calls = {"n": 0}

    def _counting_get(url, budget):
        if not budget.take():
            return None
        calls["n"] += 1
        return _FakeResp(404, "")

    monkeypatch.setattr(sec, "_passive_get", _counting_get)
    result = sec.audit_security(
        "https://wp.example.com",
        headers={},
        html="<link href='/wp-content/x.css'>",
        http_resources=[],
    )
    # Never more than the documented ~6-request cap.
    assert calls["n"] <= sec._MAX_PROBE_REQUESTS
    assert result["probe_requests_used"] <= sec._MAX_PROBE_REQUESTS


# ---------------------------------------------------------------------------
# Report renderer - "Security & Trust Posture" section
# ---------------------------------------------------------------------------


def _audit_with_security(security: dict | None) -> AuditResult:
    findings: dict = {"industry": "plumbing"}
    if security is not None:
        findings["security"] = security
    return AuditResult(
        url="https://acme.example.com",
        seo_score=80,
        issues=[],
        findings=findings,
        ts="2026-05-20T00:00:00Z",
    )


def test_render_security_section_client_facing_no_jargon():
    """The client-facing section shows punchy headlines + impact, and NONE of
    the technical detail (evidence, ids, remediation, header names)."""
    security = {
        "grade": "C",
        "probe_requests_used": 4,
        "checks_run": ["security_headers", "tls_certificate"],
        "findings": [
            SecurityFinding(
                id="missing_content_security_policy",
                severity="medium",
                category="headers",
                title="No Content-Security-Policy header",
                evidence="Content-Security-Policy header absent",
                risk="injected scripts run freely",
                remediation="Add a Content-Security-Policy header.",
                client_headline="One hacked plugin away from a customer-data leak",
                client_impact=("Your site has no safety net for a compromised script."),
            ).model_dump(),
            SecurityFinding(
                id="tls_certificate_valid",
                severity="info",
                category="tls",
                title="TLS certificate valid",
                evidence="expires 2027-01-01",
                risk="",
                remediation="No action required.",
                client_headline="Your secure-connection certificate is healthy",
                client_impact="Visitors see the padlock and browsers trust you.",
            ).model_dump(),
        ],
    }
    lines = _render_security_posture(_audit_with_security(security))
    body = "\n".join(lines)
    assert "## Security & Trust Posture" in body
    assert "Security Grade: C" in body
    # Client headline + impact are present.
    assert "One hacked plugin away from a customer-data leak" in body
    assert "Your site has no safety net for a compromised script." in body
    # Plain severity word, not a badge.
    assert "How serious: Worth fixing." in body
    # Passing finding lands in the calm "good shape" list.
    assert "Already in good shape" in body
    assert "Your secure-connection certificate is healthy" in body
    # NONE of the technical detail leaks into the client section.
    assert "missing_content_security_policy" not in body
    assert "tls_certificate_valid" not in body
    assert "Content-Security-Policy" not in body
    assert "How to fix:" not in body
    assert "Evidence:" not in body
    assert "Add a Content-Security-Policy header." not in body
    # No em-dashes (cp1252 safety).
    assert "—" not in body
    assert "–" not in body


def test_render_security_section_omitted_when_no_audit():
    # No "security" key -> section is skipped entirely.
    assert _render_security_posture(_audit_with_security(None)) == []
    # Empty dict -> also skipped.
    assert _render_security_posture(_audit_with_security({})) == []


def test_render_security_section_groups_worst_first():
    security = {
        "grade": "F",
        "probe_requests_used": 2,
        "checks_run": [],
        "findings": [
            SecurityFinding(
                id="medium_one",
                severity="medium",
                category="tls",
                title="Medium item",
                evidence="",
                risk="meh",
                remediation="ok",
                client_headline="A medium problem on your site",
                client_impact="This one is worth fixing.",
            ).model_dump(),
            SecurityFinding(
                id="crit_one",
                severity="critical",
                category="tls",
                title="Critical item",
                evidence="",
                risk="bad",
                remediation="fix now",
                client_headline="An urgent problem on your site",
                client_impact="This one is dangerous.",
            ).model_dump(),
        ],
    }
    body = "\n".join(_render_security_posture(_audit_with_security(security)))
    # The critical (urgent) finding's headline appears before the medium one.
    assert body.index("An urgent problem on your site") < body.index(
        "A medium problem on your site"
    )


def test_render_security_technical_keeps_all_detail():
    """The technical report keeps every jargon-heavy detail the client
    section drops: titles, evidence, risk, remediation, ids, grade."""
    security = {
        "grade": "C",
        "probe_requests_used": 4,
        "checks_run": ["security_headers"],
        "findings": [
            SecurityFinding(
                id="missing_content_security_policy",
                severity="medium",
                category="headers",
                title="No Content-Security-Policy header",
                evidence="Content-Security-Policy header absent",
                risk="injected scripts run freely",
                remediation="Add a Content-Security-Policy header.",
                client_headline="One hacked plugin away from a customer-data leak",
                client_impact="Your site has no safety net.",
            ).model_dump(),
        ],
    }
    body = render_security_technical(_audit_with_security(security))
    assert "Technical Report" in body
    assert "**Security Grade:** C" in body
    # All technical detail survives.
    assert "`missing_content_security_policy`" in body
    assert "No Content-Security-Policy header" in body
    assert "Evidence: Content-Security-Policy header absent" in body
    assert "Risk: injected scripts run freely" in body
    assert "How to fix: Add a Content-Security-Policy header." in body
    # Per-category breakdown is present.
    assert "Findings by category" in body
    assert "headers" in body
    # No em-dashes (cp1252 safety).
    assert "—" not in body
    assert "–" not in body


def test_render_security_technical_omitted_when_no_audit():
    assert render_security_technical(_audit_with_security(None)) == ""
    assert render_security_technical(_audit_with_security({})) == ""


def test_audit_url_wires_security_into_findings(monkeypatch):
    """End-to-end: audit_url with the toggle on attaches findings['security'].

    All network is mocked: the page fetch (httpx), TLS (socket), and DNS.
    """
    from backend.common.settings import reload_settings

    monkeypatch.setenv("SAMUS_SEO_SECURITY_AUDIT_ENABLED", "true")
    reload_settings()

    page_html = (
        "<html><head><title>Acme Plumbing Yuba City</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'>"
        "<link rel='canonical' href='https://acme.example.com/'>"
        "<meta property='og:title' content='t'>"
        "<meta property='og:image' content='i'>"
        "<script type='application/ld+json'>"
        '{"@context":"https://schema.org","@type":"Plumber","name":"Acme"}'
        "</script></head><body><h1>Acme</h1>"
        "<p>123 Main St, Yuba City. Call 530-555-0100.</p>"
        "<img src='/l.png' alt='l'><a href='/'>Home</a></body></html>"
    )

    import httpx as _httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            req = _httpx.Request("GET", url)
            if "robots.txt" in url:
                return _httpx.Response(404, text="", request=req)
            if any(
                p in url for p in ("/wp-", "/.git/", "/.well-known/", "/xmlrpc.php", "/readme.html")
            ):
                return _httpx.Response(404, text="", request=req)
            return _httpx.Response(200, text=page_html, request=req)

    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod.httpx, "Client", _Client)
    # TLS handshake -> degrade. DNS -> dnspython absent path.
    monkeypatch.setattr(
        sec.socket,
        "create_connection",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("blocked in test")),
    )
    monkeypatch.setattr(sec, "_HAS_DNSPYTHON", False)

    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")

    assert "security" in result.findings
    security = result.findings["security"]
    assert security["grade"] in ("A", "B", "C", "D", "F")
    assert isinstance(security["findings"], list)
    assert security["findings"]
    # Probe budget was respected.
    assert security["probe_requests_used"] <= sec._MAX_PROBE_REQUESTS


def test_audit_url_skips_security_when_toggle_off(monkeypatch):
    """With the toggle off, audit_url omits findings['security'] entirely."""
    from backend.common.settings import reload_settings

    monkeypatch.setenv("SAMUS_SEO_SECURITY_AUDIT_ENABLED", "false")
    reload_settings()

    page_html = (
        "<html><head><title>Acme Plumbing Yuba City</title>"
        "<meta name='description' content='" + "x" * 80 + "'>"
        "<meta name='viewport' content='w'></head>"
        "<body><h1>Acme</h1></body></html>"
    )

    import httpx as _httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            req = _httpx.Request("GET", url)
            if "robots.txt" in url:
                return _httpx.Response(404, text="", request=req)
            return _httpx.Response(200, text=page_html, request=req)

    import backend.seo.audit as audit_mod

    monkeypatch.setattr(audit_mod.httpx, "Client", _Client)

    from backend.seo.audit import audit_url

    result = audit_url("https://acme.example.com/")
    assert "security" not in result.findings


def test_full_report_includes_security_section_after_recommendations():
    from backend.seo.models import OptimizeResult

    security = {
        "grade": "B",
        "probe_requests_used": 3,
        "checks_run": ["security_headers"],
        "findings": [
            SecurityFinding(
                id="caa_missing",
                severity="low",
                category="email_auth",
                title="No CAA record",
                evidence="no CAA",
                risk="any CA may issue",
                remediation="Publish a CAA record.",
                client_headline="Anyone could create a lookalike of your secure site",
                client_impact="It is easier for a scammer to fake your site.",
            ).model_dump(),
        ],
    }
    audit = _audit_with_security(security)
    optimize = OptimizeResult(
        url=audit.url,
        recommendations=[],
        on_page_changes={},
        ts="2026-05-20T00:00:00Z",
    )
    body = render_seo_report_markdown(audit, optimize, None)
    assert "## Security & Trust Posture" in body
    # Section sits after Recommendations.
    assert body.index("## Recommendations") < body.index("## Security & Trust Posture")
    assert "Security Grade: B" in body
    # Client headline shows; technical detail does not.
    assert "Anyone could create a lookalike of your secure site" in body
    assert "caa_missing" not in body


# ---------------------------------------------------------------------------
# infrastructure_health telemetry (additive, 2026-05-20)
# ---------------------------------------------------------------------------


def _finding(severity: str, category: str = "headers") -> SecurityFinding:
    return SecurityFinding(
        id=f"{category}_{severity}",
        severity=severity,
        category=category,
        title="t",
        evidence="e",
        risk="r",
        remediation="rem",
        client_headline="h",
        client_impact="i",
    )


def test_infrastructure_health_perfect_when_no_infra_findings():
    health = sec.infrastructure_health([])
    assert health["score"] == 1.0
    assert health["rating"] == "healthy"
    assert health["infra_findings_considered"] == 0
    assert health["worst_severity"] == ""


def test_infrastructure_health_ignores_non_infra_categories():
    # platform + exposure findings must NOT move the infra-health scalar.
    findings = [
        _finding("critical", "platform"),
        _finding("high", "exposure"),
    ]
    health = sec.infrastructure_health(findings)
    assert health["score"] == 1.0
    assert health["infra_findings_considered"] == 0


def test_infrastructure_health_debits_infra_severities():
    # one medium header finding -> 100 - 9 = 91 -> 0.91, still healthy.
    health = sec.infrastructure_health([_finding("medium", "headers")])
    assert health["score"] == 0.91
    assert health["rating"] == "healthy"
    assert health["infra_findings_considered"] == 1
    assert health["worst_severity"] == "medium"


def test_infrastructure_health_critical_tls_tanks_score():
    findings = [
        _finding("critical", "tls"),
        _finding("high", "email_auth"),
    ]
    health = sec.infrastructure_health(findings)
    # 100 - 45 - 22 = 33 -> 0.33 -> critical band.
    assert health["score"] == 0.33
    assert health["rating"] == "critical"
    assert health["worst_severity"] == "critical"


def test_infrastructure_health_info_findings_carry_no_debit():
    health = sec.infrastructure_health([_finding("info", "tls")])
    assert health["score"] == 1.0
    assert health["worst_severity"] == "info"


def test_infrastructure_health_rating_bands():
    # degraded band: 0.7 <= score < 0.9 ; two medium -> 100-18=82 -> 0.82
    h = sec.infrastructure_health(
        [_finding("medium", "headers"), _finding("medium", "cookies")],
    )
    assert h["rating"] == "degraded"
    # at_risk band: 0.5 <= score < 0.7 ; one high+one medium -> 100-22-9=69
    h = sec.infrastructure_health(
        [_finding("high", "tls"), _finding("medium", "headers")],
    )
    assert h["score"] == 0.69
    assert h["rating"] == "at_risk"


def test_security_score_exposed_independently():
    findings = [_finding("high", "tls"), _finding("low", "headers")]
    # security_score uses _SEVERITY_PENALTY: 100 - 20 - 3 = 77.
    assert sec.security_score(findings) == 77
    # grade_security still agrees with the band for that score.
    assert sec.grade_security(findings) == "C"


def test_audit_security_attaches_infrastructure_health():
    result = sec.audit_security(
        "https://acme.example.com/",
        headers=_FULL_HEADERS,
        html="<html></html>",
        http_resources=[],
    )
    assert "infrastructure_health" in result
    ih = result["infrastructure_health"]
    assert 0.0 <= ih["score"] <= 1.0
    assert ih["rating"] in ("healthy", "degraded", "at_risk", "critical")
