"""Tier-1 deterministic prospect enrichment for the signal_filter workcell.

Three free, deterministic enrichment sources — no paid API keys, no LLM:

  1. **DNS / MX lookup** — ``socket.getaddrinfo`` resolves the apex domain;
     an MX record (best-effort, via ``dnspython`` if installed) signals a
     real, mail-capable business.
  2. **SSL / TLS validation** — a TLS handshake on port 443 proves the site
     serves HTTPS with a presentable certificate.
  3. **Homepage fetch** — reuses :func:`backend.prospecting.crawler.fetch_homepage`
     and :func:`backend.prospecting.seo_audit.score_seo` rather than
     reimplementing a crawler, and reuses
     :func:`backend.prospecting.enrichment.extract_owner_signals` for
     contact-surface extraction.

Everything is fail-soft: a network failure or missing dependency yields a
neutral default, never an exception. The whole module's contract is "never
raise" — callers downstream score whatever is returned.

Clearbit-style firmographic enrichment and LinkedIn presence are **pluggable
and optional** (:func:`firmographic_enrichment`). With no API key configured
they are graceful no-ops returning neutral defaults — Samus is local-first
and must not hard-require a paid API.
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
from typing import Any
from urllib.parse import urlparse

from backend.prospecting.crawler import fetch_homepage, is_dead_or_junk
from backend.prospecting.enrichment import extract_owner_signals
from backend.prospecting.seo_audit import score_seo

_LOG = logging.getLogger("samus.signal_filter.enrichment")

_DNS_TIMEOUT = 5.0
_SSL_TIMEOUT = 5.0

# Optional MX lookup — dnspython is not a guaranteed project dependency, so
# the import is guarded. Without it, MX resolution is skipped and the
# enrichment falls back to a plain A-record check.
try:  # pragma: no cover - import guard
    import dns.resolver  # type: ignore[import-untyped]

    _HAS_DNSPYTHON = True
except Exception:  # pragma: no cover - dnspython optional
    _HAS_DNSPYTHON = False


def _domain_of(url_or_domain: str) -> str:
    """Extract a bare hostname from a URL or a raw domain string."""
    raw = (url_or_domain or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").strip().lower()
    return host


def resolve_dns(domain: str) -> dict[str, Any]:
    """Resolve ``domain`` to A records + best-effort MX. Never raises.

    Returns ``{resolves: bool, has_mx: bool, mx_count: int, addresses: int}``.
    A neutral all-False/zero dict is returned on any failure.
    """
    result: dict[str, Any] = {
        "resolves": False,
        "has_mx": False,
        "mx_count": 0,
        "addresses": 0,
    }
    if not domain:
        return result

    try:
        socket.setdefaulttimeout(_DNS_TIMEOUT)
        infos = socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
        result["resolves"] = bool(infos)
        result["addresses"] = len({i[4][0] for i in infos})
    except Exception as exc:  # noqa: BLE001 - fail-soft DNS resolution
        _LOG.debug("dns resolve failed domain=%s err=%s", domain, exc)
        return result
    finally:
        socket.setdefaulttimeout(None)

    if _HAS_DNSPYTHON:
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = _DNS_TIMEOUT
            answers = resolver.resolve(domain, "MX")
            mx_count = len(list(answers))
            result["has_mx"] = mx_count > 0
            result["mx_count"] = mx_count
        except Exception as exc:  # noqa: BLE001 - MX is best-effort
            _LOG.debug("mx lookup failed domain=%s err=%s", domain, exc)

    return result


def validate_ssl(domain: str) -> dict[str, Any]:
    """Open a TLS connection on port 443 and inspect the certificate.

    Returns ``{ssl_valid: bool, has_cert: bool}``. ``ssl_valid`` is True when
    the handshake completes against the system trust store; ``has_cert`` is
    True even for a self-signed / expired cert that still presented one.
    Never raises.
    """
    result: dict[str, Any] = {"ssl_valid": False, "has_cert": False}
    if not domain:
        return result

    # First pass: a verifying handshake against the default trust store.
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=_SSL_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        result["ssl_valid"] = True
        result["has_cert"] = bool(cert)
        return result
    except ssl.SSLError as exc:
        _LOG.debug("ssl verify failed domain=%s err=%s", domain, exc)
    except Exception as exc:  # noqa: BLE001 - connect/timeout/etc
        _LOG.debug("ssl connect failed domain=%s err=%s", domain, exc)
        return result

    # Second pass: the cert exists but did not verify (self-signed/expired).
    # Still worth recording — a present cert is a stronger signal than none.
    try:
        unverified = ssl._create_unverified_context()  # noqa: SLF001
        with socket.create_connection((domain, 443), timeout=_SSL_TIMEOUT) as sock:
            with unverified.wrap_socket(sock, server_hostname=domain) as tls:
                result["has_cert"] = tls.getpeercert(binary_form=True) is not None
    except Exception as exc:  # noqa: BLE001 - fail-soft
        _LOG.debug("ssl unverified probe failed domain=%s err=%s", domain, exc)

    return result


def fetch_site(url: str) -> dict[str, Any]:
    """Fetch the prospect homepage and run the prospecting SEO heuristic.

    Reuses :func:`backend.prospecting.crawler.fetch_homepage` and
    :func:`backend.prospecting.seo_audit.score_seo` rather than
    reimplementing a crawler. Returns
    ``{reachable, dead_or_junk, seo_score, seo_issues, owner_signals}``.
    Never raises.
    """
    result: dict[str, Any] = {
        "reachable": False,
        "dead_or_junk": True,
        "seo_score": 0,
        "seo_issues": ["no_html"],
        "owner_signals": {},
    }
    if not url:
        return result

    try:
        page = fetch_homepage(url)
        result["reachable"] = int(page.get("status_code") or 0) == 200
        result["dead_or_junk"] = is_dead_or_junk(page)
        seo_score, seo_issues = score_seo(page)
        result["seo_score"] = seo_score
        result["seo_issues"] = seo_issues
        html = page.get("html")
        if isinstance(html, str) and html:
            result["owner_signals"] = extract_owner_signals(html, base_url=url)
    except Exception as exc:  # noqa: BLE001 - fail-soft homepage fetch
        _LOG.debug("homepage fetch failed url=%s err=%s", url, exc)

    return result


# --- Pluggable / optional enrichment ---------------------------------------
#
# Clearbit-style firmographics and LinkedIn presence need a paid API key.
# Samus is local-first: when no key is configured this is a graceful no-op
# returning neutral defaults so scoring proceeds without it.

_NEUTRAL_FIRMOGRAPHICS: dict[str, Any] = {
    "available": False,
    "employee_count": 0,
    "estimated_revenue": 0,
    "has_linkedin": False,
    "tech_stack_size": 0,
}


def firmographic_enrichment(
    domain: str,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Optional Clearbit-style firmographic + LinkedIn enrichment.

    ``api_key`` overrides the ``CLEARBIT_API_KEY`` env lookup (useful for
    tests). When neither is set this returns ``_NEUTRAL_FIRMOGRAPHICS`` — a
    no-op that lets deterministic scoring proceed without paid data.

    The provider call itself is intentionally **not** implemented: wiring a
    paid API is an operator decision, and this workcell must never hard-
    require one. A future operator can replace the ``return`` below with a
    real provider client; the ``available`` flag already gates scoring.
    """
    key = (api_key if api_key is not None else os.getenv("CLEARBIT_API_KEY") or "").strip()
    if not key or not domain:
        return dict(_NEUTRAL_FIRMOGRAPHICS)
    # A paid provider is configured but deliberately left unwired — see the
    # docstring. Returning the neutral default keeps the gate deterministic
    # and offline until an operator opts in explicitly.
    _LOG.debug("firmographic enrichment key present but provider unwired; neutral default")
    return dict(_NEUTRAL_FIRMOGRAPHICS)


def enrich(prospect: dict[str, Any], *, firmographics_api_key: str | None = None) -> dict[str, Any]:
    """Run the full Tier-1 enrichment cascade for one prospect. Never raises.

    ``prospect`` is the dict form of :class:`~backend.signal_filter.models.ProspectInput`.
    Returns a flat enrichment dict consumed by
    :func:`backend.signal_filter.scoring.signals_from_enrichment`.
    """
    website = str(prospect.get("website_url") or "")
    domain = _domain_of(website)

    dns_info = resolve_dns(domain)
    ssl_info = validate_ssl(domain)
    site_info = fetch_site(website)
    firmographics = firmographic_enrichment(domain, api_key=firmographics_api_key)

    return {
        "domain": domain,
        "has_website": bool(domain),
        "dns": dns_info,
        "ssl": ssl_info,
        "site": site_info,
        "firmographics": firmographics,
        # Echo a few input-side signals scoring also consumes.
        "review_rating": str(prospect.get("review_rating") or ""),
        "review_count": str(prospect.get("review_count") or ""),
        "phone": str(prospect.get("phone") or ""),
    }


__all__ = [
    "resolve_dns",
    "validate_ssl",
    "fetch_site",
    "firmographic_enrichment",
    "enrich",
]
