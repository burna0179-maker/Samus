"""Passive security & trust-posture audit for the customer-facing SEO report.

This module runs as an enrichment step inside ``backend.seo.audit.audit_url``
and produces a client-facing "Security & Trust Posture" section: a list of
:class:`SecurityFinding` records plus an overall A-F grade.

PASSIVE-AUDIT CONTRACT (non-negotiable)
---------------------------------------
This audit runs against *clients'* live websites. It MUST stay strictly
passive and non-exploitative:

  * Only benign HTTP GET requests, one TLS handshake, and DNS lookups.
  * No authentication attempts, no credential guessing, no brute-force.
  * No injection payloads, no fuzzing, no POST/PUT/DELETE - GET only.
  * No exploitation of anything discovered; presence is observed, never used.
  * Total *extra* HTTP requests are capped at ``_MAX_PROBE_REQUESTS`` (~6)
    with short per-request timeouts so a client site is never stressed.

Every probe is a request a normal browser or search-engine crawler would
make. The homepage response (status, html, headers) is fetched once by the
SEO audit itself and reused here - this module never re-fetches it.

LOCAL-FIRST / DETERMINISTIC
---------------------------
Zero LLM calls. Samus runs a hard $1/day LLM cap; this feature must not
consume any of it. Every check is deterministic pure-ish logic over data
fetched with short timeouts. Every network/DNS/TLS call is wrapped so a
failure degrades to a clear finding and never raises out of the audit - the
SEO audit must still succeed if the security audit fails entirely.
"""

from __future__ import annotations

import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.common.shared_http import get_shared_client

from backend.common import safe_fetch

from .models import SecurityFinding

_LOG = logging.getLogger("samus.seo.security_audit")

# --- passive-probe budget --------------------------------------------------
# Hard cap on EXTRA HTTP requests this module issues (the homepage fetch is
# done by the SEO audit and reused, so it is not counted here). The probe
# helpers below increment a per-run counter and refuse once the cap is hit.
_MAX_PROBE_REQUESTS = 6
# Phase-split budget (2026-07-01 hang fix). A scalar 5s is defeated by a
# slow-trickle read — a probed WordPress/artifact endpoint that ACCEPTs then
# dribbles bytes resets the scalar read deadline forever. Cap connect/read/
# write/pool independently so no passive probe can wedge the audit. The
# security probes hit small endpoints (wp-json, xmlrpc, readme, .git/config)
# so a 5s read window is ample.
_PROBE_TIMEOUT_S: httpx.Timeout = safe_fetch.bounded_timeout(
    connect=5.0,
    read=5.0,
    write=5.0,
    pool=5.0,
)
_TLS_TIMEOUT_S = 5.0
_DNS_TIMEOUT_S = 5.0
# Browser-equivalent identity — a bot UA is 403'd by common WAFs, which would
# turn every passive probe into a false "could not observe" finding. Shared
# with the SEO page fetch + the prospecting crawler (the 2026-05-21 sweep).
_USER_AGENT = safe_fetch.BROWSER_USER_AGENT

# Days-remaining threshold below which a TLS certificate is flagged.
_TLS_EXPIRY_WARN_DAYS = 21

# Optional dependency. dnspython is listed in requirements.txt; the import is
# guarded so a missing install degrades to one "could not resolve DNS"
# finding rather than crashing the whole SEO audit.
try:  # pragma: no cover - import guard
    import dns.resolver  # type: ignore[import-untyped]

    _HAS_DNSPYTHON = True
except Exception:  # pragma: no cover - dnspython optional at runtime
    dns = None  # type: ignore[assignment]
    _HAS_DNSPYTHON = False


# ---------------------------------------------------------------------------
# Probe budget guard
# ---------------------------------------------------------------------------


class _ProbeBudget:
    """Tracks remaining passive HTTP GET budget for one audit run."""

    def __init__(self, limit: int = _MAX_PROBE_REQUESTS) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        """Consume one request slot. Returns False once the cap is reached."""
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _passive_get(url: str, budget: _ProbeBudget) -> httpx.Response | None:
    """Issue ONE benign HTTP GET within the probe budget.

    Returns the :class:`httpx.Response` on success, or ``None`` if the budget
    is exhausted or the request failed. Never raises - a failed probe simply
    means "could not observe", which the callers translate into a finding.
    """
    if not budget.take():
        _LOG.debug("security probe budget exhausted, skipping url=%s", url)
        return None
    try:
        # EC2-NEW-01 — the probe URL is built from a prospect-supplied
        # homepage; validate it (and refuse redirect hops, which bypass the
        # pre-check) so a poisoned/redirecting URL cannot reach IMDS /
        # loopback / RFC-1918. Mirrors the crawler's NET-01 guard.
        safe_fetch.assert_public_http_url(url)
        client = get_shared_client(
            timeout=_PROBE_TIMEOUT_S,
            follow_redirects=False,
            http2=safe_fetch.HTTP2_AVAILABLE,
        )
        return client.get(url, headers=dict(safe_fetch.BROWSER_HEADERS))
    except safe_fetch.SsrfBlockedError as exc:
        _LOG.warning("security probe blocked by SSRF guard url=%s err=%s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - passive probe, never blocks
        _LOG.debug("security probe failed url=%s err=%s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Check 1 - HTTP security headers
# ---------------------------------------------------------------------------


def check_security_headers(headers: dict[str, str]) -> list[SecurityFinding]:
    """Audit HTTP security headers from the already-fetched homepage response.

    ``headers`` is the lower-cased header dict the SEO audit's ``_fetch``
    already produced for the homepage. This check issues NO network requests.
    """
    findings: list[SecurityFinding] = []
    # Normalize keys defensively even though _fetch lower-cases them.
    h = {k.lower(): v for k, v in (headers or {}).items()}

    csp = h.get("content-security-policy", "").strip()
    if not csp:
        findings.append(
            SecurityFinding(
                id="missing_content_security_policy",
                severity="medium",
                category="headers",
                title="No Content-Security-Policy header",
                evidence="Content-Security-Policy header absent",
                risk=(
                    "Without a Content-Security-Policy, a single injected script "
                    "tag (via a compromised plugin or ad) can run freely and "
                    "steal customer data or deface the page."
                ),
                remediation=(
                    "Add a Content-Security-Policy response header. Start in "
                    "report-only mode (Content-Security-Policy-Report-Only) to "
                    "find what loads, then enforce a policy that allows only "
                    "your own domain and trusted CDNs."
                ),
                client_headline="One hacked plugin away from a customer-data leak",
                client_impact=(
                    "Your site has no safety net. The moment a plugin, an ad, or "
                    "an embedded script is compromised - and on WordPress that is "
                    "a matter of when, not if - malicious code runs free on your "
                    "pages and can quietly scrape everything your visitors type, "
                    "including their contact details."
                ),
            )
        )

    hsts = h.get("strict-transport-security", "").strip()
    if not hsts:
        findings.append(
            SecurityFinding(
                id="missing_strict_transport_security",
                severity="medium",
                category="headers",
                title="No HSTS (Strict-Transport-Security) header",
                evidence="Strict-Transport-Security header absent",
                risk=(
                    "Without HSTS, a visitor on public Wi-Fi can be silently "
                    "downgraded to plain HTTP, letting an attacker read or alter "
                    "the page before it loads."
                ),
                remediation=(
                    "Add 'Strict-Transport-Security: max-age=31536000; "
                    "includeSubDomains' once you have confirmed every subdomain "
                    "serves HTTPS. Add 'preload' and submit to hstspreload.org "
                    "for the strongest protection."
                ),
                client_headline="Your site can be pushed onto an unsafe connection",
                client_impact=(
                    "Nothing forces visitors onto the secure version of your "
                    "site. On a coffee-shop or hotel network, an attacker can "
                    "quietly slip your customers onto an unprotected copy of your "
                    "pages, read everything they type, and tamper with what they "
                    "see - all without a single warning."
                ),
            )
        )
    else:
        hsts_lower = hsts.lower()
        gaps: list[str] = []
        if "includesubdomains" not in hsts_lower:
            gaps.append("includeSubDomains")
        if "preload" not in hsts_lower:
            gaps.append("preload")
        if gaps:
            findings.append(
                SecurityFinding(
                    id="weak_strict_transport_security",
                    severity="low",
                    category="headers",
                    title="HSTS is set but missing " + " and ".join(gaps),
                    evidence=f"Strict-Transport-Security: {hsts}",
                    risk=(
                        "HSTS protects the bare domain but the missing directives "
                        "leave subdomains (or the very first visit) reachable over "
                        "plain HTTP and open to a downgrade."
                    ),
                    remediation=(
                        "Extend the header to 'max-age=31536000; includeSubDomains; "
                        "preload' after confirming all subdomains serve HTTPS, "
                        "then submit the domain at hstspreload.org."
                    ),
                    client_headline="Your visitors' first move on public Wi-Fi is exposed",
                    client_impact=(
                        "Coffee shop, airport, hotel lobby - the instant someone "
                        "opens your site on an untrusted network, that very first "
                        "connection can be quietly intercepted before your site's "
                        "protection takes hold."
                    ),
                )
            )

    xfo = h.get("x-frame-options", "").strip()
    csp_lower = csp.lower()
    has_frame_ancestors = "frame-ancestors" in csp_lower
    if not xfo and not has_frame_ancestors:
        findings.append(
            SecurityFinding(
                id="missing_clickjacking_protection",
                severity="medium",
                category="headers",
                title="No clickjacking protection",
                evidence="X-Frame-Options absent and no CSP frame-ancestors directive",
                risk=(
                    "Without frame protection an attacker can load your site "
                    "invisibly inside their own page and trick your customers "
                    "into clicking buttons they cannot see (clickjacking)."
                ),
                remediation=(
                    "Add 'X-Frame-Options: SAMEORIGIN' or, preferably, a "
                    "Content-Security-Policy with 'frame-ancestors \\'self\\''. "
                    "Use DENY if the site is never framed by anyone."
                ),
                client_headline="Scammers can wear your website like a mask",
                client_impact=(
                    "Your site can be loaded invisibly inside a fraudster's page, "
                    "so your customers think they are clicking you while they are "
                    "actually clicking the attacker. Your brand becomes the bait "
                    "in someone else's scam, and you never even see it happen."
                ),
            )
        )

    xcto = h.get("x-content-type-options", "").strip().lower()
    if xcto != "nosniff":
        findings.append(
            SecurityFinding(
                id="missing_x_content_type_options",
                severity="low",
                category="headers",
                title="No X-Content-Type-Options: nosniff",
                evidence=(
                    f"X-Content-Type-Options: {xcto}"
                    if xcto
                    else "X-Content-Type-Options header absent"
                ),
                risk=(
                    "Without 'nosniff' a browser may guess the type of an "
                    "uploaded file and run it as a script, turning a harmless "
                    "upload into an attack."
                ),
                remediation="Add the response header 'X-Content-Type-Options: nosniff'.",
                client_headline="A harmless-looking upload could run as an attack",
                client_impact=(
                    "Browsers are allowed to guess what an uploaded file really "
                    "is. That means an image a visitor uploads could be treated "
                    "as program code and run on your pages, handing an attacker a "
                    "foothold from something that looked completely innocent."
                ),
            )
        )

    referrer = h.get("referrer-policy", "").strip()
    if not referrer:
        findings.append(
            SecurityFinding(
                id="missing_referrer_policy",
                severity="low",
                category="headers",
                title="No Referrer-Policy header",
                evidence="Referrer-Policy header absent",
                risk=(
                    "Without a Referrer-Policy the full URL of your pages "
                    "(which can contain private query parameters) leaks to every "
                    "third-party site your visitors click through to."
                ),
                remediation=(
                    "Add 'Referrer-Policy: strict-origin-when-cross-origin' "
                    "(a safe, widely-compatible default)."
                ),
                client_headline="Your private page links leak to outside sites",
                client_impact=(
                    "Every time a visitor clicks a link off your site, the exact "
                    "page they came from is handed to that other site. If your "
                    "links carry private details - a customer name, a quote "
                    "reference - those details quietly travel to strangers you "
                    "never chose to share them with."
                ),
            )
        )

    permissions = h.get("permissions-policy", "").strip()
    if not permissions:
        findings.append(
            SecurityFinding(
                id="missing_permissions_policy",
                severity="low",
                category="headers",
                title="No Permissions-Policy header",
                evidence="Permissions-Policy header absent",
                risk=(
                    "Without a Permissions-Policy, injected or third-party "
                    "scripts can quietly request the camera, microphone, or "
                    "location of your visitors."
                ),
                remediation=(
                    "Add a Permissions-Policy header that disables features the "
                    "site does not use, e.g. "
                    "'Permissions-Policy: geolocation=(), camera=(), microphone=()'."
                ),
                client_headline="Outside scripts can reach for your visitors' camera",
                client_impact=(
                    "Your site does not lock down sensitive device features, so a "
                    "compromised or third-party script can quietly ask your "
                    "visitors for their camera, microphone, or location. That is "
                    "the kind of prompt that frightens customers away and makes "
                    "them question whether your business can be trusted."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Check 6 - cookie flags + mixed content (from data already in hand)
# ---------------------------------------------------------------------------


def check_cookie_flags(
    headers: dict[str, str],
    page_url: str,
) -> list[SecurityFinding]:
    """Flag insecure Set-Cookie headers from the already-fetched response.

    Issues NO network requests. ``Set-Cookie`` may appear once per cookie;
    httpx folds repeated headers, so the value is split on newlines.
    """
    findings: list[SecurityFinding] = []
    h = {k.lower(): v for k, v in (headers or {}).items()}
    raw = h.get("set-cookie", "")
    if not raw:
        return findings

    is_https = urlparse(page_url).scheme == "https"
    # httpx joins repeated Set-Cookie headers with newlines.
    cookie_lines = [c for c in raw.split("\n") if c.strip()]
    insecure: list[str] = []
    for line in cookie_lines:
        low = line.lower()
        name = line.split("=", 1)[0].strip() or "(unnamed)"
        gaps: list[str] = []
        if is_https and "secure" not in low:
            gaps.append("Secure")
        if "httponly" not in low:
            gaps.append("HttpOnly")
        if gaps:
            insecure.append(f"{name} (missing {', '.join(gaps)})")

    if insecure:
        findings.append(
            SecurityFinding(
                id="insecure_cookie_flags",
                severity="medium",
                category="cookies",
                title="Cookies set without Secure/HttpOnly flags",
                evidence="; ".join(insecure[:5]),
                risk=(
                    "A cookie without HttpOnly can be read by injected JavaScript "
                    "and a cookie without Secure can leak over plain HTTP, either "
                    "of which lets an attacker hijack a logged-in session."
                ),
                remediation=(
                    "Set every cookie with the 'HttpOnly' and 'Secure' flags (and "
                    "'SameSite=Lax' or 'Strict'). For WordPress this is usually a "
                    "one-line change in wp-config.php or a security plugin setting."
                ),
                client_headline="Someone could slip into a logged-in account",
                client_impact=(
                    "The little tokens that keep people signed in to your site "
                    "are not locked down. An attacker who grabs one can step "
                    "straight into that person's account - including your own "
                    "admin login - without ever needing the password."
                ),
            )
        )
    return findings


def check_mixed_content(
    page_url: str,
    http_resources: list[str],
) -> list[SecurityFinding]:
    """Flag http:// sub-resources referenced by an https:// page.

    Uses the ``http_resources_on_https`` list the SEO audit already extracted
    from the homepage HTML. Issues NO network requests.
    """
    findings: list[SecurityFinding] = []
    if urlparse(page_url).scheme != "https":
        return findings
    resources = [r for r in (http_resources or []) if r]
    if resources:
        findings.append(
            SecurityFinding(
                id="mixed_content_resources",
                severity="medium",
                category="cookies",
                title="Secure page loads insecure (http://) resources",
                evidence=", ".join(resources[:3]),
                risk=(
                    "An https:// page that pulls in http:// scripts or images "
                    "lets an attacker on the network modify those resources, "
                    "and browsers show the page as 'Not fully secure'."
                ),
                remediation=(
                    "Change every http:// asset reference to https:// (or a "
                    "protocol-relative // URL). Most modern hosts serve the same "
                    "asset over HTTPS already."
                ),
                client_headline="Your secure pages quietly pull in unsafe content",
                client_impact=(
                    "Parts of your page load over an unprotected connection, so "
                    "an attacker on the same network can swap them out for "
                    "anything they like. Browsers also flag the page as 'not "
                    "fully secure', which is exactly the warning that makes a "
                    "potential customer click away."
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 2 - TLS certificate
# ---------------------------------------------------------------------------


def check_tls_certificate(hostname: str) -> list[SecurityFinding]:
    """Perform ONE TLS handshake and inspect the served certificate.

    Reports issuer, expiry date, and days remaining. Warns when fewer than
    ``_TLS_EXPIRY_WARN_DAYS`` days remain. Never raises: a handshake failure
    degrades to one informational finding.
    """
    if not hostname:
        return [
            SecurityFinding(
                id="tls_check_skipped",
                severity="info",
                category="tls",
                title="TLS certificate not checked",
                evidence="no hostname could be derived from the audited URL",
                risk="",
                remediation="No action required; the audited URL had no hostname.",
                client_headline="Your security certificate was not checked this time",
                client_impact=(
                    "We could not work out a web address to test, so this one "
                    "item was simply skipped. It is not a problem with your site - "
                    "just re-run the report with your full website address."
                ),
            )
        ]

    try:
        context = ssl.create_default_context()
        with socket.create_connection(
            (hostname, 443),
            timeout=_TLS_TIMEOUT_S,
        ) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
    except Exception as exc:  # noqa: BLE001 - passive probe, never blocks
        _LOG.debug("tls handshake failed host=%s err=%s", hostname, exc)
        return [
            SecurityFinding(
                id="tls_handshake_failed",
                severity="info",
                category="tls",
                title="Could not complete a TLS handshake",
                evidence=f"connection to {hostname}:443 failed ({type(exc).__name__})",
                risk=(
                    "The site's HTTPS certificate could not be inspected; if the "
                    "site has no working HTTPS at all, browsers will warn every "
                    "visitor away."
                ),
                remediation=(
                    "Confirm the site is reachable over HTTPS on port 443. If you "
                    "have no certificate, install a free one (most hosts and "
                    "Let's Encrypt provide them at no cost)."
                ),
                client_headline="We could not reach your site's secure connection",
                client_impact=(
                    "The secure version of your site did not answer when we tried "
                    "to check it. If visitors hit the same wall, they see a "
                    "browser warning instead of your business - so it is worth "
                    "confirming with your web host that the padlock works."
                ),
            )
        ]

    if not cert:
        # getpeercert() returns {} when the cert is untrusted/unverified.
        return [
            SecurityFinding(
                id="tls_certificate_unverified",
                severity="high",
                category="tls",
                title="TLS certificate could not be verified",
                evidence=f"{hostname}:443 served a certificate that did not validate",
                risk=(
                    "An untrusted or self-signed certificate makes browsers show "
                    "a full-page security warning, and customers will not trust "
                    "the site enough to enter contact or payment details."
                ),
                remediation=(
                    "Install a certificate from a trusted authority (Let's "
                    "Encrypt is free and automatic on most hosts) and make sure "
                    "the full certificate chain is served."
                ),
                client_headline="Visitors get a scary security warning instead of your site",
                client_impact=(
                    "Browsers do not trust your site's certificate, so customers "
                    "see a full-page red warning telling them your site is not "
                    "safe. Almost nobody clicks past that screen - to them, your "
                    "business simply looks broken or fake."
                ),
            )
        ]

    issuer = _format_cert_name(cert.get("issuer"))
    not_after_raw = cert.get("notAfter", "")
    expiry = _parse_cert_datetime(not_after_raw)
    findings: list[SecurityFinding] = []

    if expiry is None:
        findings.append(
            SecurityFinding(
                id="tls_certificate_unreadable_expiry",
                severity="info",
                category="tls",
                title="TLS certificate expiry could not be read",
                evidence=f"issuer={issuer}; notAfter={not_after_raw!r}",
                risk="",
                remediation=(
                    "No action required; the certificate is present but its "
                    "expiry date was in an unexpected format."
                ),
                client_headline="Your certificate's renewal date was not readable",
                client_impact=(
                    "Your site's security certificate is in place, but we could "
                    "not read its expiry date this time. There is nothing for you "
                    "to fix - just keep automatic renewal switched on so it never "
                    "lapses."
                ),
            )
        )
        return findings

    days_remaining = (expiry - datetime.now(timezone.utc)).days
    expiry_str = expiry.strftime("%Y-%m-%d")

    if days_remaining < 0:
        findings.append(
            SecurityFinding(
                id="tls_certificate_expired",
                severity="critical",
                category="tls",
                title="TLS certificate has EXPIRED",
                evidence=f"issuer={issuer}; expired {expiry_str} ({abs(days_remaining)} days ago)",
                risk=(
                    "An expired certificate triggers a full-page browser security "
                    "warning for every visitor, so the site is effectively down "
                    "and customers cannot reach it."
                ),
                remediation=(
                    "Renew the certificate immediately. Enable automatic renewal "
                    "(Let's Encrypt / certbot, or your host's auto-renew setting) "
                    "so this never recurs."
                ),
                client_headline="Your site is showing a full-page danger warning right now",
                client_impact=(
                    "Your security certificate has lapsed, so every single "
                    "visitor is met with a red full-screen warning that your site "
                    "is unsafe. For all practical purposes your website is down "
                    "and turning customers away - this needs fixing today."
                ),
            )
        )
    elif days_remaining < _TLS_EXPIRY_WARN_DAYS:
        findings.append(
            SecurityFinding(
                id="tls_certificate_expiring_soon",
                severity="high",
                category="tls",
                title=f"TLS certificate expires in {days_remaining} days",
                evidence=f"issuer={issuer}; expires {expiry_str}",
                risk=(
                    "If the certificate lapses before it is renewed, every "
                    "visitor sees a security warning and the site looks broken."
                ),
                remediation=(
                    "Renew the certificate now and turn on automatic renewal so "
                    "it cannot lapse unattended."
                ),
                client_headline="Your site's safety badge expires in days",
                client_impact=(
                    "The certificate that powers your padlock is about to run "
                    "out. If it lapses before it is renewed, every visitor "
                    "suddenly sees a full-page warning and your site looks "
                    "broken - so this needs handling before the deadline, not "
                    "after."
                ),
            )
        )
    else:
        findings.append(
            SecurityFinding(
                id="tls_certificate_valid",
                severity="info",
                category="tls",
                title=f"TLS certificate valid ({days_remaining} days remaining)",
                evidence=f"issuer={issuer}; expires {expiry_str}",
                risk="",
                remediation=(
                    "No action required. Confirm automatic renewal is enabled so "
                    "the certificate cannot lapse unnoticed."
                ),
                client_headline="Your secure-connection certificate is healthy",
                client_impact=(
                    "Visitors see the padlock and browsers trust your site. Keep "
                    "automatic renewal switched on so it never lapses."
                ),
            )
        )
    return findings


def _format_cert_name(name_field: Any) -> str:
    """Flatten an ssl getpeercert() issuer/subject tuple-of-tuples to text."""
    if not name_field:
        return "unknown"
    parts: list[str] = []
    try:
        for rdn in name_field:
            for key, value in rdn:
                if key in ("organizationName", "commonName"):
                    parts.append(str(value))
    except Exception:  # noqa: BLE001 - defensive against odd cert shapes
        return "unknown"
    return " / ".join(dict.fromkeys(parts)) or "unknown"


def _parse_cert_datetime(raw: str) -> datetime | None:
    """Parse an ssl notAfter string ('Jun  1 12:00:00 2026 GMT') to UTC."""
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
        return parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Check 3 - email authentication DNS
# ---------------------------------------------------------------------------


def probe_email_auth(domain: str) -> list[SecurityFinding]:
    """Inspect SPF, DMARC, DKIM and CAA DNS records for ``domain``.

    Uses dnspython. If dnspython is not installed OR resolution fails, a
    single "could not resolve DNS" finding is returned (graceful degrade).
    Never raises.
    """
    if not domain:
        return [
            SecurityFinding(
                id="email_auth_check_skipped",
                severity="info",
                category="email_auth",
                title="Email authentication not checked",
                evidence="no domain could be derived from the audited URL",
                risk="",
                remediation="No action required; the audited URL had no domain.",
                client_headline="Your email-spoofing protection was not checked",
                client_impact=(
                    "We could not work out a domain name to test, so this one "
                    "item was skipped. It is not a fault with your site - just "
                    "re-run the report with your full website address."
                ),
            )
        ]

    if not _HAS_DNSPYTHON:
        return [
            SecurityFinding(
                id="email_auth_dns_unavailable",
                severity="info",
                category="email_auth",
                title="Email authentication could not be checked",
                evidence="DNS resolver library (dnspython) is not installed",
                risk="",
                remediation=(
                    "This is an audit-tooling note, not a site problem. The DNS "
                    "resolver was unavailable when the report ran; re-run the "
                    "audit once it is installed for SPF/DMARC/DKIM coverage."
                ),
                client_headline="Your email-spoofing protection was not checked",
                client_impact=(
                    "The tool that inspects email protection was not available "
                    "when this report ran. This is a note about our checking "
                    "tools, not a problem on your side - we will cover it on the "
                    "next run."
                ),
            )
        ]

    txt_apex = _resolve_txt(domain)
    if txt_apex is None:
        # Apex TXT lookup itself failed - DNS is unreachable for this domain.
        return [
            SecurityFinding(
                id="email_auth_dns_unresolved",
                severity="info",
                category="email_auth",
                title="Could not resolve DNS for the domain",
                evidence=f"TXT lookup for {domain} did not return a result",
                risk="",
                remediation=(
                    "The domain's DNS could not be reached when the audit ran. "
                    "Re-run the audit; if it keeps failing, check that the "
                    "domain's nameservers are responding."
                ),
                client_headline="Your email-spoofing protection was not checked",
                client_impact=(
                    "Your domain's directory did not respond when we tried to "
                    "look it up, so the email checks were skipped this time. "
                    "Re-running the report usually clears it up."
                ),
            )
        ]

    findings: list[SecurityFinding] = []
    findings.extend(_audit_spf(domain, txt_apex))
    findings.extend(_audit_dmarc(domain))
    findings.extend(_audit_dkim(domain))
    findings.extend(_audit_caa(domain))
    return findings


def _resolve_txt(name: str) -> list[str] | None:
    """Resolve TXT records for ``name``. Returns [] when the name has none,
    or ``None`` when the lookup itself failed (network / nameserver error)."""
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = _DNS_TIMEOUT_S
        resolver.timeout = _DNS_TIMEOUT_S
        answers = resolver.resolve(name, "TXT")
        records: list[str] = []
        for rdata in answers:
            # TXT rdata is a sequence of byte strings; join + decode.
            parts = getattr(rdata, "strings", None)
            if parts is not None:
                records.append(b"".join(parts).decode("utf-8", "replace"))
            else:
                records.append(str(rdata).strip('"'))
        return records
    except dns.resolver.NXDOMAIN:
        return []
    except dns.resolver.NoAnswer:
        return []
    except Exception as exc:  # noqa: BLE001 - lookup failure, degrade gracefully
        _LOG.debug("TXT resolve failed name=%s err=%s", name, exc)
        return None


def _audit_spf(domain: str, txt_apex: list[str]) -> list[SecurityFinding]:
    spf = next((r for r in txt_apex if r.lower().startswith("v=spf1")), "")
    if not spf:
        return [
            SecurityFinding(
                id="spf_missing",
                severity="medium",
                category="email_auth",
                title="No SPF record published",
                evidence=f"no v=spf1 TXT record on {domain}",
                risk=(
                    "Without an SPF record, anyone can send email that appears to "
                    "come from your domain, which fuels phishing of your "
                    "customers and hurts your real email's deliverability."
                ),
                remediation=(
                    "Publish an SPF TXT record listing the services allowed to "
                    "send your mail, ending in '-all', e.g. "
                    "'v=spf1 include:_spf.google.com -all'."
                ),
                client_headline="Strangers can send email in your name unchecked",
                client_impact=(
                    "Your domain has nothing that tells the world who is allowed "
                    "to send email as you. That makes it easy for a scammer to "
                    "pose as your business when emailing clients, and it makes "
                    "your own genuine emails more likely to land in the spam "
                    "folder."
                ),
            )
        ]
    findings: list[SecurityFinding] = []
    spf_low = spf.lower()
    if spf_low.rstrip().endswith("+all") or " +all" in spf_low:
        findings.append(
            SecurityFinding(
                id="spf_permissive_all",
                severity="high",
                category="email_auth",
                title="SPF record ends in '+all' (allows anyone)",
                evidence=spf,
                risk=(
                    "An SPF record ending in '+all' authorizes the whole internet "
                    "to send mail as your domain, which is worse than having no "
                    "SPF at all."
                ),
                remediation=(
                    "Change the trailing '+all' to '-all' (hard fail) once you "
                    "have listed every legitimate sending service."
                ),
                client_headline="Your email setup invites the whole world to impersonate you",
                client_impact=(
                    "Your domain's email rule is set wide open - it actively "
                    "tells every mail server on earth that anyone is allowed to "
                    "send email as your business. A scammer could not ask for an "
                    "easier way to pose as you to your clients."
                ),
            )
        )
    elif "~all" not in spf_low and "-all" not in spf_low:
        findings.append(
            SecurityFinding(
                id="spf_no_terminating_all",
                severity="low",
                category="email_auth",
                title="SPF record has no terminating 'all' mechanism",
                evidence=spf,
                risk=(
                    "Without a terminating '-all' or '~all', receiving mail "
                    "servers have no instruction for unlisted senders, weakening "
                    "the protection SPF is meant to give."
                ),
                remediation=(
                    "End the SPF record with '-all' (recommended) or '~all' so "
                    "unlisted senders are rejected or marked as suspicious."
                ),
                client_headline="Your email protection is set up but left unfinished",
                client_impact=(
                    "You started locking down who can email as your business, but "
                    "the rule was never finished off. As it stands, mail servers "
                    "are given no clear instruction about unknown senders, so the "
                    "protection you intended is not actually taking effect."
                ),
            )
        )
    else:
        findings.append(
            SecurityFinding(
                id="spf_present",
                severity="info",
                category="email_auth",
                title="SPF record is published",
                evidence=spf,
                risk="",
                remediation=(
                    "No action required. Review the listed senders whenever you "
                    "add or remove an email service."
                ),
                client_headline="You control who can send email in your name",
                client_impact=(
                    "Your domain tells mail servers which services are allowed to "
                    "send email as your business. Review that list whenever you "
                    "add or drop an email tool."
                ),
            )
        )
    return findings


def _audit_dmarc(domain: str) -> list[SecurityFinding]:
    dmarc_records = _resolve_txt(f"_dmarc.{domain}")
    if dmarc_records is None:
        return [
            SecurityFinding(
                id="dmarc_dns_unresolved",
                severity="info",
                category="email_auth",
                title="DMARC record could not be checked",
                evidence=f"_dmarc.{domain} lookup failed",
                risk="",
                remediation="Re-run the audit; the DMARC DNS lookup did not respond.",
                client_headline="One email-protection item was not checked",
                client_impact=(
                    "One of the email-spoofing checks did not get an answer when "
                    "this report ran. It is not a fault on your site - re-running "
                    "the report usually settles it."
                ),
            )
        ]
    dmarc = next(
        (r for r in dmarc_records if r.lower().startswith("v=dmarc1")),
        "",
    )
    if not dmarc:
        return [
            SecurityFinding(
                id="dmarc_missing",
                severity="high",
                category="email_auth",
                title="No DMARC record published",
                evidence=f"no v=DMARC1 TXT record on _dmarc.{domain}",
                risk=(
                    "Without DMARC, attackers can spoof your exact domain in "
                    "phishing emails and you get no visibility or enforcement "
                    "against it, directly eroding customer trust in your brand."
                ),
                remediation=(
                    "Publish a DMARC TXT record at _dmarc.<domain>. Start with "
                    "'v=DMARC1; p=none; rua=mailto:you@<domain>' to collect "
                    "reports, then tighten to 'p=quarantine' and finally 'p=reject'."
                ),
                client_headline="Anyone can send email pretending to be you",
                client_impact=(
                    "With no protection on your domain's email, a scammer can "
                    "send messages that look exactly as if they came from you - "
                    "to your clients, your leads, your closing partners. You "
                    "would never see it, and the damage lands on your name."
                ),
            )
        ]
    policy = _dmarc_policy(dmarc)
    if policy == "none":
        return [
            SecurityFinding(
                id="dmarc_policy_none",
                severity="medium",
                category="email_auth",
                title="DMARC policy is 'p=none' (monitoring only, no enforcement)",
                evidence=dmarc,
                risk=(
                    "A 'p=none' policy tells receivers to take no action on "
                    "spoofed mail, so attackers can still impersonate your domain "
                    "even though DMARC is published."
                ),
                remediation=(
                    "Once your DMARC reports confirm legitimate mail passes, "
                    "raise the policy to 'p=quarantine' and then 'p=reject' to "
                    "actually block spoofed email."
                ),
                client_headline="Your email shield is switched on but not blocking anything",
                client_impact=(
                    "Your domain watches for scammers impersonating you, but it "
                    "is set only to watch - not to stop them. Fake emails sent in "
                    "your name still sail straight through to your customers' "
                    "inboxes."
                ),
            )
        ]
    return [
        SecurityFinding(
            id="dmarc_enforced",
            severity="info",
            category="email_auth",
            title=f"DMARC record is published with enforcement (p={policy})",
            evidence=dmarc,
            risk="",
            remediation=(
                "No action required. Keep monitoring DMARC aggregate reports for "
                "new legitimate senders."
            ),
            client_headline="Fake emails sent in your name get blocked",
            client_impact=(
                "Your domain actively stops scammers who try to email your "
                "customers while posing as you. This is exactly the protection "
                "every business should have - keep it in place."
            ),
        )
    ]


def _dmarc_policy(dmarc_record: str) -> str:
    """Extract the 'p=' policy value from a DMARC TXT record."""
    for tag in dmarc_record.split(";"):
        tag = tag.strip()
        if tag.lower().startswith("p="):
            return tag.split("=", 1)[1].strip().lower()
    return "none"


# Common DKIM selectors used by major mail providers. A passive DKIM probe
# can only confirm presence for known selectors - DKIM has no apex record.
_DKIM_SELECTORS = ("default", "google", "selector1", "selector2", "k1", "mail")


def _audit_dkim(domain: str) -> list[SecurityFinding]:
    found_selector = ""
    for selector in _DKIM_SELECTORS:
        records = _resolve_txt(f"{selector}._domainkey.{domain}")
        if records:
            for rec in records:
                if "v=dkim1" in rec.lower() or "p=" in rec.lower():
                    found_selector = selector
                    break
        if found_selector:
            break

    if found_selector:
        return [
            SecurityFinding(
                id="dkim_present",
                severity="info",
                category="email_auth",
                title="A DKIM signing key was found",
                evidence=f"DKIM record at {found_selector}._domainkey.{domain}",
                risk="",
                remediation=("No action required. DKIM is signing your outbound mail."),
                client_headline="Your outgoing email carries a tamper-proof seal",
                client_impact=(
                    "Your emails are signed with a digital seal that proves they "
                    "genuinely came from you and were not altered in transit. "
                    "That helps your mail land in the inbox and stay trusted."
                ),
            )
        ]
    return [
        SecurityFinding(
            id="dkim_not_detected",
            severity="low",
            category="email_auth",
            title="No DKIM signing key detected on common selectors",
            evidence=(
                f"no DKIM TXT record at the common selectors checked ({', '.join(_DKIM_SELECTORS)})"
            ),
            risk=(
                "Without DKIM your outbound email is not cryptographically "
                "signed, so receiving servers are more likely to treat it as spam "
                "and DMARC enforcement is weaker."
            ),
            remediation=(
                "Enable DKIM signing in your email provider's admin console "
                "(Google Workspace, Microsoft 365, etc.) and publish the DKIM "
                "TXT record it gives you. If DKIM uses a custom selector, this "
                "passive check may simply not have found it."
            ),
            client_headline="Your emails go out without a proof-of-origin seal",
            client_impact=(
                "Your outgoing email does not appear to carry a digital seal that "
                "proves it really came from you. Without it, your messages are "
                "more likely to be dumped in spam, and it is easier for someone "
                "to forge mail in your name."
            ),
        )
    ]


def _audit_caa(domain: str) -> list[SecurityFinding]:
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = _DNS_TIMEOUT_S
        resolver.timeout = _DNS_TIMEOUT_S
        answers = resolver.resolve(domain, "CAA")
        has_caa = len(list(answers)) > 0
    except dns.resolver.NXDOMAIN:
        has_caa = False
    except dns.resolver.NoAnswer:
        has_caa = False
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        _LOG.debug("CAA resolve failed domain=%s err=%s", domain, exc)
        return [
            SecurityFinding(
                id="caa_dns_unresolved",
                severity="info",
                category="email_auth",
                title="CAA record could not be checked",
                evidence=f"CAA lookup for {domain} did not respond",
                risk="",
                remediation="Re-run the audit; the CAA DNS lookup did not respond.",
                client_headline="One certificate-control item was not checked",
                client_impact=(
                    "One check about who is allowed to issue certificates for "
                    "your domain did not get an answer this time. It is not a "
                    "fault on your site - re-running the report usually clears it."
                ),
            )
        ]

    if has_caa:
        return [
            SecurityFinding(
                id="caa_present",
                severity="info",
                category="email_auth",
                title="A CAA record restricts who can issue certificates",
                evidence=f"CAA record published on {domain}",
                risk="",
                remediation="No action required. CAA is limiting certificate issuance.",
                client_headline="Only your chosen provider can vouch for your site",
                client_impact=(
                    "You have locked down who is allowed to issue the security "
                    "certificate behind your padlock. That makes it much harder "
                    "for anyone to create a fraudulent stand-in for your site."
                ),
            )
        ]
    return [
        SecurityFinding(
            id="caa_missing",
            severity="low",
            category="email_auth",
            title="No CAA record (any authority may issue a certificate)",
            evidence=f"no CAA record on {domain}",
            risk=(
                "Without a CAA record, any certificate authority in the world can "
                "issue an HTTPS certificate for your domain, making a "
                "fraudulent certificate harder to prevent."
            ),
            remediation=(
                "Publish a CAA DNS record naming only the certificate authority "
                "you use, e.g. '0 issue \"letsencrypt.org\"'."
            ),
            client_headline="Anyone could create a lookalike of your secure site",
            client_impact=(
                "You have not said who is allowed to vouch for your website's "
                "padlock, so any provider in the world could be tricked into "
                "creating one for your domain. That makes it easier for a scammer "
                "to stand up a convincing fake of your site."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Check 4 - CMS / platform exposure (WordPress)
# ---------------------------------------------------------------------------


def detect_wordpress(html: str, headers: dict[str, str]) -> bool:
    """Best-effort WordPress detection from already-fetched homepage data.

    Issues NO network requests. Signals: the WP REST API Link header, a
    generator meta tag, or wp-content / wp-includes asset paths in the HTML.
    """
    h = {k.lower(): v for k, v in (headers or {}).items()}
    link_header = h.get("link", "").lower()
    if "api.w.org" in link_header:
        return True
    body = (html or "").lower()
    if 'name="generator"' in body and "wordpress" in body:
        return True
    if "/wp-content/" in body or "/wp-includes/" in body:
        return True
    return False


def check_platform_exposure(
    base_origin: str,
    html: str,
    headers: dict[str, str],
    budget: _ProbeBudget,
) -> list[SecurityFinding]:
    """If the site runs WordPress, run a few benign GETs for known exposures.

    Each probe is a single GET a normal browser could make:
      * ``/wp-json/wp/v2/users``  - user enumeration
      * ``/xmlrpc.php``           - legacy XML-RPC surface
      * ``/readme.html``          - version-disclosing readme
    Probes share the run-wide ``budget``; nothing exploitative is attempted.
    """
    findings: list[SecurityFinding] = []
    if not detect_wordpress(html, headers):
        return findings

    findings.append(
        SecurityFinding(
            id="platform_wordpress_detected",
            severity="info",
            category="platform",
            title="Site is running WordPress",
            evidence="WordPress signatures present in homepage headers/markup",
            risk="",
            remediation=(
                "No action required for the detection itself. Keep WordPress "
                "core, themes, and plugins updated, and remove plugins you no "
                "longer use - they are the most common entry point for attacks."
            ),
            client_headline="Your site runs on WordPress, so keep it updated",
            client_impact=(
                "Your site is built on WordPress, the most popular - and most "
                "targeted - website platform. There is nothing wrong with that, "
                "but it means staying on top of updates and removing plugins you "
                "no longer use is what keeps attackers out."
            ),
        )
    )

    # --- user enumeration via the REST API -------------------------------
    users_resp = _passive_get(f"{base_origin}/wp-json/wp/v2/users", budget)
    if users_resp is not None and users_resp.status_code == 200:
        names = _extract_wp_usernames(users_resp.text)
        if names:
            findings.append(
                SecurityFinding(
                    id="wordpress_user_enumeration",
                    severity="medium",
                    category="platform",
                    title="WordPress leaks usernames via the REST API",
                    evidence=f"/wp-json/wp/v2/users exposed: {', '.join(names[:5])}",
                    risk=(
                        "The public REST API hands attackers your real login "
                        "usernames, removing the guesswork from a password-"
                        "guessing attack on the admin account."
                    ),
                    remediation=(
                        "Block or restrict the /wp-json/wp/v2/users endpoint "
                        "(most security plugins - Wordfence, iThemes, Solid "
                        "Security - have a one-click option) and rename the "
                        "default 'admin' account."
                    ),
                    client_headline="Your site is handing out the login names attackers need",
                    client_impact=(
                        "Anyone visiting your site can pull a list of the "
                        "usernames that sign in to manage it. That hands an "
                        "attacker the hardest half of breaking into your admin "
                        "account - now all they have to do is guess the password."
                    ),
                )
            )

    # --- xmlrpc.php -------------------------------------------------------
    xmlrpc_resp = _passive_get(f"{base_origin}/xmlrpc.php", budget)
    if xmlrpc_resp is not None and xmlrpc_resp.status_code in (200, 405):
        # A live xmlrpc.php answers a bare GET with 405 (method not allowed)
        # or a 200 "XML-RPC server accepts POST requests only" page.
        body_low = (xmlrpc_resp.text or "").lower()
        if xmlrpc_resp.status_code == 405 or "xml-rpc server" in body_low:
            findings.append(
                SecurityFinding(
                    id="wordpress_xmlrpc_enabled",
                    severity="medium",
                    category="platform",
                    title="WordPress XML-RPC interface is enabled",
                    evidence=f"/xmlrpc.php responded with HTTP {xmlrpc_resp.status_code}",
                    risk=(
                        "xmlrpc.php lets attackers amplify password-guessing with "
                        "system.multicall and abuse pingback for denial-of-"
                        "service attacks against other sites."
                    ),
                    remediation=(
                        "Disable XML-RPC unless an app needs it (Jetpack, the "
                        "WordPress mobile app). Most security plugins disable it "
                        "with one click, or block /xmlrpc.php at the web server."
                    ),
                    client_headline="An old back door lets attackers pound your login",
                    client_impact=(
                        "Your site still has a legacy back door switched on that "
                        "almost no modern business uses. Attackers love it - it "
                        "lets them try thousands of password guesses at once and "
                        "even rope your site into attacks on other websites."
                    ),
                )
            )

    # --- readme.html -----------------------------------------------------
    readme_resp = _passive_get(f"{base_origin}/readme.html", budget)
    if (
        readme_resp is not None
        and readme_resp.status_code == 200
        and "wordpress" in (readme_resp.text or "").lower()
    ):
        findings.append(
            SecurityFinding(
                id="wordpress_readme_exposed",
                severity="low",
                category="platform",
                title="WordPress readme.html is publicly reachable",
                evidence=f"{base_origin}/readme.html returned HTTP 200",
                risk=(
                    "readme.html can disclose the WordPress version, helping an "
                    "attacker pick exploits that match your exact install."
                ),
                remediation=(
                    "Delete /readme.html after each update or block access to it "
                    "at the web server. Keeping WordPress current matters more "
                    "than hiding the version."
                ),
                client_headline="Your site openly advertises which version it runs",
                client_impact=(
                    "A public file on your site reveals exactly which version of "
                    "WordPress you are on. That hands an attacker a shopping list "
                    "of known weaknesses to try against your exact setup."
                ),
            )
        )

    return findings


def _extract_wp_usernames(body: str) -> list[str]:
    """Pull 'slug'/'name' values from a WP /users REST API JSON response."""
    import json as _json

    try:
        data = _json.loads(body or "")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("slug") or entry.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


# ---------------------------------------------------------------------------
# Check 5 - exposed artifacts
# ---------------------------------------------------------------------------


def check_exposed_artifacts(
    base_origin: str,
    budget: _ProbeBudget,
) -> list[SecurityFinding]:
    """Two benign GETs: /.well-known/security.txt and /.git/config.

    security.txt absence is informational (a recommendation to add one).
    A reachable /.git/config is a real high-severity exposure.
    """
    findings: list[SecurityFinding] = []

    sec_txt = _passive_get(f"{base_origin}/.well-known/security.txt", budget)
    if sec_txt is not None and sec_txt.status_code == 200 and sec_txt.text.strip():
        findings.append(
            SecurityFinding(
                id="security_txt_present",
                severity="info",
                category="exposure",
                title="A security.txt contact file is published",
                evidence=f"{base_origin}/.well-known/security.txt is reachable",
                risk="",
                remediation=(
                    "No action required. Keep the contact and expiry fields in "
                    "security.txt current."
                ),
                client_headline="People can reach you if they spot a problem",
                client_impact=(
                    "Your site publishes a clear way for a helpful researcher to "
                    "tell you about a security issue. It is a small, professional "
                    "trust signal - just keep the contact details current."
                ),
            )
        )
    else:
        findings.append(
            SecurityFinding(
                id="security_txt_missing",
                severity="info",
                category="exposure",
                title="No security.txt contact file",
                evidence=f"{base_origin}/.well-known/security.txt not found",
                risk=(
                    "Without a security.txt file, a researcher who spots a "
                    "problem on your site has no clear, trusted way to report it "
                    "to you."
                ),
                remediation=(
                    "Add a plain-text file at /.well-known/security.txt listing a "
                    "contact address and an expiry date. This is a low-effort "
                    "trust signal; see securitytxt.org for the format."
                ),
                client_headline="A small, optional trust signal you could add",
                client_impact=(
                    "Your site has no published way for a well-meaning expert to "
                    "warn you about a problem. This is not a fault - just a nice, "
                    "low-effort polish that makes your business look that bit "
                    "more professional."
                ),
            )
        )

    git_cfg = _passive_get(f"{base_origin}/.git/config", budget)
    if git_cfg is not None and git_cfg.status_code == 200 and "[core]" in (git_cfg.text or ""):
        findings.append(
            SecurityFinding(
                id="git_directory_exposed",
                severity="high",
                category="exposure",
                title="The site's .git directory is publicly reachable",
                evidence=f"{base_origin}/.git/config returned a Git config file",
                risk=(
                    "An exposed .git directory lets anyone download your entire "
                    "source code history, which often contains passwords, API "
                    "keys, and database credentials."
                ),
                remediation=(
                    "Block all access to the /.git/ path at the web server "
                    "immediately, then rotate any credentials that were ever "
                    "committed to the repository."
                ),
                client_headline="Your site's inner blueprints are downloadable by anyone",
                client_impact=(
                    "A behind-the-scenes folder is exposed that lets a stranger "
                    "download the full build history of your website. These files "
                    "very often contain passwords and database keys - in effect, "
                    "the keys to your site are sitting on the doormat."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

# Deterministic penalty weights per severity. 'info' carries no penalty -
# it is a recommendation or a passing check, not a defect.
_SEVERITY_PENALTY = {
    "critical": 40,
    "high": 20,
    "medium": 8,
    "low": 3,
    "info": 0,
}


def security_score(findings: list[SecurityFinding]) -> int:
    """Deterministic 0-100 security score behind :func:`grade_security`.

    Score = 100 minus the summed severity penalties, clamped to 0-100.
    Exposed separately so the telemetry layer can read the raw scalar
    without re-deriving it from the A-F grade band.
    """
    score = 100
    for finding in findings:
        score -= _SEVERITY_PENALTY.get(finding.severity, 0)
    return max(0, min(100, score))


def grade_security(findings: list[SecurityFinding]) -> str:
    """Derive a deterministic A-F grade from the finding severities.

    Score = 100 minus the summed severity penalties (clamped to 0-100).
    Grade bands: A >=90, B >=80, C >=70, D >=60, F <60.
    """
    score = security_score(findings)
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Infrastructure-health telemetry (additive, 2026-05-20)
# ---------------------------------------------------------------------------
# A single 0..1 scalar summarising the trust posture of the audited site's
# *infrastructure* — TLS, DNS-published email authentication, and HTTP
# transport headers. Unlike the A-F security grade (which scores ALL findings,
# including platform exposure + exposed-artifact probes), infrastructure_health
# is narrowed to the operational plumbing an operator can monitor as a health
# signal. Derived purely from findings already produced by the audit — no new
# network calls. Pure + deterministic; never raises.

# Categories that count as "infrastructure" for the health scalar. 'platform'
# (WordPress exposure) and 'exposure' (.git, security.txt) are deliberately
# excluded — they are content/config defects, not transport-layer health.
_INFRA_CATEGORIES: frozenset[str] = frozenset({"tls", "email_auth", "headers", "cookies"})

# Per-severity health debit, expressed on the same 0..100 scale the security
# score uses, then normalised to 0..1. Mirrors _SEVERITY_PENALTY but is kept
# separate so the health scalar can be retuned without moving the A-F grade.
_INFRA_SEVERITY_DEBIT: dict[str, int] = {
    "critical": 45,
    "high": 22,
    "medium": 9,
    "low": 3,
    "info": 0,
}


def infrastructure_health(findings: list[SecurityFinding]) -> dict[str, Any]:
    """Derive a 0..1 infrastructure-health scalar from existing findings.

    Looks ONLY at TLS / email-auth / HTTP-header / cookie findings — the
    transport-and-trust plumbing — and subtracts a per-severity debit from a
    perfect 100, then normalises to 0..1. ``info`` findings carry no debit
    (a passing check or a recommendation, not a defect).

    Returns a serialisable dict::

        {"score": 0.0..1.0,          # rounded to 3 dp
         "rating": "healthy"|"degraded"|"at_risk"|"critical",
         "infra_findings_considered": int,
         "worst_severity": str}      # worst infra severity seen, "" if none

    Never raises — a malformed finding list yields a perfect-health default
    so this can never sink the SEO audit.
    """
    try:
        infra = [f for f in (findings or []) if getattr(f, "category", "") in _INFRA_CATEGORIES]
        score_100 = 100
        worst = ""
        _rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        worst_rank = -1
        for f in infra:
            sev = getattr(f, "severity", "info")
            score_100 -= _INFRA_SEVERITY_DEBIT.get(sev, 0)
            if _rank.get(sev, -1) > worst_rank:
                worst_rank, worst = _rank.get(sev, -1), sev
        score_100 = max(0, min(100, score_100))
        scalar = round(score_100 / 100.0, 3)
        if scalar >= 0.9:
            rating = "healthy"
        elif scalar >= 0.7:
            rating = "degraded"
        elif scalar >= 0.5:
            rating = "at_risk"
        else:
            rating = "critical"
        return {
            "score": scalar,
            "rating": rating,
            "infra_findings_considered": len(infra),
            "worst_severity": worst,
        }
    except Exception as exc:  # noqa: BLE001 - telemetry must never block the audit
        _LOG.warning("infrastructure_health derivation failed: %s", exc)
        return {
            "score": 1.0,
            "rating": "healthy",
            "infra_findings_considered": 0,
            "worst_severity": "",
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def audit_security(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    html: str = "",
    http_resources: list[str] | None = None,
) -> dict[str, Any]:
    """Run the full passive security audit and return a serializable dict.

    Reuses the homepage response (``headers``, ``html``, ``http_resources``)
    that ``backend.seo.audit`` already fetched - it never re-fetches the
    homepage. Extra HTTP GETs (WordPress + artifact probes) are bounded by a
    shared ~6-request budget. Returns:

        {"grade": "A".."F",
         "findings": [<SecurityFinding dict>, ...],
         "probe_requests_used": int,
         "checks_run": [<check name>, ...]}

    Never raises: any check that fails internally is caught here so the SEO
    audit can attach a partial result and still succeed.
    """
    headers = headers or {}
    http_resources = http_resources or []
    parsed = urlparse(url if "://" in url else f"https://{url}")
    hostname = parsed.hostname or ""
    domain = hostname[4:] if hostname.startswith("www.") else hostname
    scheme = parsed.scheme or "https"
    base_origin = f"{scheme}://{hostname}" if hostname else ""

    budget = _ProbeBudget()
    findings: list[SecurityFinding] = []
    checks_run: list[str] = []

    def _run(name: str, fn) -> None:
        """Run one check, absorbing any failure into an info finding."""
        try:
            findings.extend(fn())
            checks_run.append(name)
        except Exception as exc:  # noqa: BLE001 - one check must not sink the audit
            _LOG.warning("security check %s failed: %s", name, exc)
            findings.append(
                SecurityFinding(
                    id=f"{name}_check_error",
                    severity="info",
                    category="exposure",
                    title=f"The {name.replace('_', ' ')} check could not complete",
                    evidence=f"{type(exc).__name__}",
                    risk="",
                    remediation=(
                        "This is an audit-tooling note, not a confirmed site "
                        "problem. Re-run the audit; if it keeps failing, contact "
                        "your auditor."
                    ),
                    client_headline="One of the security checks did not finish",
                    client_impact=(
                        "One check could not be completed when this report ran. "
                        "It is a hiccup in our tooling, not a confirmed problem "
                        "on your site - re-running the report usually settles it."
                    ),
                )
            )

    _run("security_headers", lambda: check_security_headers(headers))
    _run("cookie_flags", lambda: check_cookie_flags(headers, url))
    _run("mixed_content", lambda: check_mixed_content(url, http_resources))
    _run("tls_certificate", lambda: check_tls_certificate(hostname))
    _run("email_auth", lambda: probe_email_auth(domain))
    if base_origin:
        _run(
            "platform_exposure",
            lambda: check_platform_exposure(base_origin, html, headers, budget),
        )
        _run(
            "exposed_artifacts",
            lambda: check_exposed_artifacts(base_origin, budget),
        )

    grade = grade_security(findings)
    return {
        "grade": grade,
        # Telemetry (additive, 2026-05-20): a 0..1 infrastructure-health
        # scalar over the TLS / email-auth / header / cookie findings — the
        # transport-and-trust plumbing — derived with zero extra network
        # calls so the operator gets a single monitorable health number.
        "infrastructure_health": infrastructure_health(findings),
        "findings": [f.model_dump() for f in findings],
        "probe_requests_used": budget.used,
        "checks_run": checks_run,
    }
