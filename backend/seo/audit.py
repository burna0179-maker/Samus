"""Deterministic SEO audit via httpx + BeautifulSoup (optional).

Performs an HTTP GET on the target URL, then builds a flat list of
:class:`SeoIssue` records from a fixed checklist. ``seo_score`` is derived
from the issue severities (100 - 5*critical - 3*high - 1*medium, clamped).

Local-first: zero LLM calls, no external API keys.

Enrichment surface (added 2026-05-16) — beyond the original 6-field check:
  - canonical URL
  - Open Graph (og:title / og:description / og:image / og:type / og:url)
  - schema.org JSON-LD presence + @type list + LocalBusiness detection
  - alt-text coverage % across <img> tags
  - internal vs external link graph + unique anchor count
  - analytics presence (GA4 / GTM / Meta Pixel)
  - lazy-loading coverage on <img> tags

All enrichment is bs4-only; the regex fallback returns empty defaults for
these fields so ``_build_issues`` can be tolerant without branching.
"""
from __future__ import annotations

import json as _json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from backend.common import safe_fetch
from backend.common.config import get_settings
from backend.common.dates import iso_now

from .models import AuditResult, SeoIssue
from .pagespeed_client import PageSpeedResult, audit_pagespeed
from .security_audit import audit_security

_LOG = logging.getLogger("samus.seo.audit")

# Phase-split transport budget (2026-07-01 hang fix). A scalar float is
# defeated by a slow-trickle read — an origin that accepts the connection then
# dribbles one byte at a time (the observed stalled Facebook ``:443`` read)
# keeps resetting a scalar read timeout forever. ``bounded_timeout`` caps
# connect/read/write/pool independently so no single warm/hot audit fetch
# (homepage + robots.txt) can wedge the sequential daily run.
_HTTP_TIMEOUT = safe_fetch.bounded_timeout(
    connect=5.0, read=15.0, write=10.0, pool=5.0,
)
# Browser-equivalent identity. A self-identifying bot UA is 403'd outright by
# common WAFs (the 2026-05-21 crawler false-positive sweep), which sinks the
# audit of a perfectly healthy customer site. _USER_AGENT is kept as a name
# for the robots.txt can_fetch() call; live requests send the full
# safe_fetch.BROWSER_HEADERS set.
_USER_AGENT = safe_fetch.BROWSER_USER_AGENT


def _client_factory(**kwargs):
    """``safe_get`` client factory — negotiates HTTP/2 when the ``h2`` package
    is installed, else HTTP/1.1. References the module ``httpx`` so the audit
    test suite's monkeypatches of ``audit.httpx.Client`` still apply.

    Retained for back-compat / fallback only. Hot calls in this module now go
    through :func:`_get_shared_client`, which reuses a single keep-alive
    client across audits to amortize the TCP+TLS handshake.
    """
    return httpx.Client(http2=safe_fetch.HTTP2_AVAILABLE, **kwargs)


# Module-local keep-alive client cache (perf hoist 2026-05-26). One audit
# fetches the homepage + robots.txt, and an operator/morning-brief batch
# fans across many prospects — repeatedly constructing+tearing down
# ``httpx.Client`` per call sinks tens of thousands of sockets/day. We
# cache a single ``follow_redirects=False`` client and reuse it.
#
# Crucially, the cache is keyed on the *current* ``httpx.Client`` class
# identity (read off this module's ``httpx`` attribute, not the real
# ``httpx`` module). When the SEO test suites monkeypatch
# ``backend.seo.audit.httpx`` with a ``_FakeHttpx`` whose ``.Client`` is a
# stub, the cached real-httpx client is invalidated and a fresh stub
# instance is built — preserving the existing per-test isolation surface.
_SHARED_CLIENT: httpx.Client | None = None
_SHARED_CLIENT_CLASS: type | None = None


def _get_shared_client() -> httpx.Client:
    """Return the module's reusable ``httpx.Client``.

    Lazily constructed; rebuilt whenever ``httpx.Client`` identity changes
    (the monkeypatch-respecting path used by the audit test suite). Lifetime
    is the process — ``atexit`` is not registered here because the cached
    real client is short enough to not warrant teardown noise; tests' stub
    clients get torn down naturally when their fixture exits because the
    next call sees a different ``httpx.Client`` and supersedes the cache.
    """
    global _SHARED_CLIENT, _SHARED_CLIENT_CLASS
    Client = httpx.Client
    if _SHARED_CLIENT is None or _SHARED_CLIENT_CLASS is not Client:
        # Close the previous real client cleanly if we are rotating it out
        # (tests rotate constantly; production effectively never does).
        prev = _SHARED_CLIENT
        if prev is not None:
            try:
                prev.close()
            except Exception:  # noqa: BLE001 — rotation never raises
                pass
        # follow_redirects=False is mandatory: safe_fetch.safe_get walks
        # redirect hops manually so it can re-run the SSRF guard on every
        # Location. The shared client must not short-circuit that.
        is_real = getattr(Client, "__module__", "").startswith("httpx")
        kwargs: dict[str, Any] = {
            "timeout": _HTTP_TIMEOUT,
            "follow_redirects": False,
        }
        if is_real:
            kwargs["http2"] = safe_fetch.HTTP2_AVAILABLE
        _SHARED_CLIENT = Client(**kwargs)
        _SHARED_CLIENT_CLASS = Client
    return _SHARED_CLIENT

# Retry budget for the page fetch + robots.txt fetch. Transient 5xx +
# connect/read timeouts get retried; permanent 4xx are not. Three total
# attempts, exponential backoff 0.5s -> 1s -> 2s.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_MIN_S = 0.5
_RETRY_BACKOFF_MAX_S = 4.0

try:  # optional dependency
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    _HAS_BS4 = True
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]
    _HAS_BS4 = False

try:  # tenacity is in requirements.txt; keep an import guard for safety
    from tenacity import (  # type: ignore[import-untyped]
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
    _HAS_TENACITY = True
except Exception:  # pragma: no cover
    _HAS_TENACITY = False


# Retry on transport errors + 5xx. Permanent 4xx (404, 403) do NOT retry —
# they're real outcomes the audit should report.
_RETRYABLE_HTTPX = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.HTTPStatusError,  # raised by _fetch_inner on 5xx
)


def _fetch_inner(url: str) -> tuple[int, str, dict[str, str]]:
    """One fetch attempt. Raises on transient failure so the retry wrapper
    can decide whether to back off. 4xx returns normally (real outcome).

    Routed through ``safe_fetch.safe_get`` (finding H3): the SSRF egress guard
    runs before the request and again on every redirect hop, so an
    attacker-supplied URL can never be used to reach instance metadata,
    loopback, or RFC1918 sibling workcells. The reusable keep-alive client
    from :func:`_get_shared_client` is handed to ``safe_get`` via its
    ``client=`` parameter so a batch audit amortizes one TCP+TLS handshake
    across many fetches; the audit test suite's monkeypatches of
    ``audit.httpx.Client`` still apply because ``_get_shared_client``
    re-derives its client off the current ``httpx.Client`` identity. A
    blocked URL raises :class:`safe_fetch.SsrfBlockedError`, which is *not*
    in the retry-allowed set and propagates straight to ``_fetch`` for a
    clean ``fetch_failed``.
    """
    r = safe_fetch.safe_get(
        url,
        timeout=_HTTP_TIMEOUT,
        headers=dict(safe_fetch.BROWSER_HEADERS),
        client=_get_shared_client(),
    )
    if 500 <= r.status_code < 600:
        raise httpx.HTTPStatusError(
            f"server returned {r.status_code}", request=r.request, response=r,
        )
    return r.status_code, r.text or "", {k.lower(): v for k, v in r.headers.items()}


if _HAS_TENACITY:
    _fetch_retried = retry(
        stop=stop_after_attempt(_RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=_RETRY_BACKOFF_MIN_S,
            min=_RETRY_BACKOFF_MIN_S,
            max=_RETRY_BACKOFF_MAX_S,
        ),
        retry=retry_if_exception_type(_RETRYABLE_HTTPX),
        reraise=True,
    )(_fetch_inner)
else:  # pragma: no cover — tenacity is a hard requirement, but defensive
    _fetch_retried = _fetch_inner


def _fetch(url: str) -> tuple[int, str, dict[str, str]]:
    """GET ``url``; return ``(status_code, html, headers)``. Empty on error.

    Retries up to 3 times on transient failures (5xx, ConnectError,
    ReadTimeout, RemoteProtocolError) with exponential backoff. Permanent
    4xx codes return as-is — they're real outcomes the audit reports.

    A :class:`safe_fetch.SsrfBlockedError` (the URL pointed at a non-public /
    metadata address — finding H3) is caught here and degraded to the same
    ``(0, "", {})`` empty result as any other fetch failure, so ``audit_url``
    surfaces a clean ``fetch_failed`` issue instead of a 500.
    """
    try:
        return _fetch_retried(url)
    except safe_fetch.SsrfBlockedError as exc:
        _LOG.warning("audit fetch blocked by SSRF guard url=%s err=%s", url, exc)
        return 0, "", {}
    except httpx.HTTPError as exc:
        _LOG.warning("audit fetch failed url=%s err=%s", url, exc)
        return 0, "", {}


def _robots_url(target_url: str) -> str:
    """Resolve the robots.txt URL for a target page's origin."""
    parsed = urlparse(target_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _check_robots_txt(target_url: str) -> tuple[str, bool]:
    """Fetch + parse robots.txt for the target URL's origin.

    Returns ``(status, allows_us)``:
      - ``status`` ∈ {"allows", "disallows", "missing", "error"}
      - ``allows_us`` is True iff our user-agent can fetch ``target_url``

    Conservative defaults: missing robots.txt → treated as allowing
    (standard web behavior). robots.txt fetch failure → treated as
    allowing (we can't penalize on a flaky origin server).
    """
    import urllib.robotparser

    robots_url = _robots_url(target_url)
    if not robots_url:
        return "error", True
    try:
        # Same SSRF egress guard as the page fetch (finding H3): robots.txt is
        # derived from the same untrusted origin, so it is fetched through
        # safe_fetch too. A blocked URL degrades to the conservative
        # ("error", True) — robots fetch failure never penalizes the prospect.
        r = safe_fetch.safe_get(
            robots_url,
            timeout=_HTTP_TIMEOUT,
            headers=dict(safe_fetch.BROWSER_HEADERS),
            client=_get_shared_client(),
        )
    except (httpx.HTTPError, safe_fetch.SsrfBlockedError):
        return "error", True
    if r.status_code == 404:
        return "missing", True
    if r.status_code >= 400:
        return "error", True

    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    allowed = rp.can_fetch(_USER_AGENT, target_url)
    return ("allows" if allowed else "disallows"), allowed


def _extract_canonical(soup) -> str:
    """Read the canonical URL from ``<link rel="canonical" href="...">``."""
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        # bs4 returns rel as a list; lowercase + check membership
        rel_lower = [r.lower() for r in (rel if isinstance(rel, list) else [rel])]
        if "canonical" in rel_lower:
            return (link.get("href") or "").strip()
    return ""


def _extract_open_graph(soup) -> dict[str, str]:
    """Read all ``<meta property="og:*">`` tags into a flat dict.

    Stripped of the ``og:`` prefix on the way out: ``og:title`` -> ``title``.
    """
    og: dict[str, str] = {}
    for m in soup.find_all("meta"):
        prop = (m.get("property") or "").lower()
        if prop.startswith("og:"):
            key = prop[3:]  # drop 'og:'
            og[key] = (m.get("content") or "").strip()
    return og


def _extract_schema_org(soup) -> tuple[list[str], bool, bool]:
    """Parse every ``<script type="application/ld+json">`` block.

    Returns ``(types, has_local_business, has_organization)``:
      - ``types`` is the flat list of ``@type`` values across all blocks.
        Handles single dict, list of dicts, and @graph-wrapped lists.
      - ``has_local_business`` is True if any @type contains 'LocalBusiness'
        or a known LocalBusiness subtype (Restaurant, Plumber, Dentist etc).
      - ``has_organization`` is True if any @type is 'Organization' or
        'LocalBusiness' (the canonical NAP-bearing entities).

    Malformed JSON in any single block is silently dropped — one bad
    block must not invalidate the others.
    """
    # LocalBusiness subtypes per schema.org spec — sample of the most
    # likely-encountered ones for small-business prospects. Substring
    # match is good enough; schema.org allows custom subtypes anyway.
    local_business_subtypes = {
        "LocalBusiness", "AutoRepair", "BarOrPub", "BeautySalon", "Bakery",
        "ChildCare", "DaySpa", "Dentist", "DryCleaningOrLaundry",
        "Electrician", "EmergencyService", "EmploymentAgency",
        "FinancialService", "FoodEstablishment", "GeneralContractor",
        "HVACBusiness", "HairSalon", "HomeAndConstructionBusiness",
        "HousePainter", "LegalService", "Locksmith", "MedicalBusiness",
        "MovingCompany", "NailSalon", "Notary", "Optician", "Pediatric",
        "Pharmacy", "Physician", "Plumber", "RealEstateAgent", "Restaurant",
        "RoofingContractor", "SelfStorage", "Store", "TaxiService",
        "TravelAgency", "VeterinaryCare",
    }

    types: list[str] = []
    has_local_business = False
    has_organization = False

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        # Normalize: schema.org JSON-LD can be a dict, a list of dicts,
        # or a dict with @graph: [list of dicts]. Flatten to a list of dicts.
        candidates: list[dict] = []
        if isinstance(parsed, dict):
            graph = parsed.get("@graph")
            if isinstance(graph, list):
                candidates.extend([x for x in graph if isinstance(x, dict)])
            else:
                candidates.append(parsed)
        elif isinstance(parsed, list):
            candidates.extend([x for x in parsed if isinstance(x, dict)])
        for node in candidates:
            t = node.get("@type")
            t_list = t if isinstance(t, list) else [t] if t else []
            for raw_type in t_list:
                if not isinstance(raw_type, str):
                    continue
                # Some schemas namespace the type (schema.org/Plumber);
                # take the final path segment.
                clean = raw_type.rsplit("/", 1)[-1].strip()
                if not clean:
                    continue
                types.append(clean)
                if clean in local_business_subtypes:
                    has_local_business = True
                if clean in ("Organization", "LocalBusiness"):
                    has_organization = True
    return types, has_local_business, has_organization


def _extract_image_signals(soup) -> tuple[int, int, int]:
    """Return ``(image_count, images_with_alt, images_with_lazy_load)``."""
    image_count = 0
    with_alt = 0
    with_lazy = 0
    for img in soup.find_all("img"):
        image_count += 1
        # alt attribute exists AND is non-empty
        alt = img.get("alt")
        if alt is not None and alt.strip():
            with_alt += 1
        loading = (img.get("loading") or "").strip().lower()
        if loading == "lazy":
            with_lazy += 1
    return image_count, with_alt, with_lazy


def _extract_link_graph(soup, base_url: str) -> tuple[int, int, int, int]:
    """Return ``(internal_count, external_count, nofollow_count, unique_anchors)``.

    Internal = same host as ``base_url`` OR a relative href. External =
    different host. Anchors are normalized via str.strip().lower(); only
    non-empty anchors count toward unique_anchors.
    """
    base_host = urlparse(base_url).netloc.lower()
    internal = external = nofollow = 0
    anchors: set[str] = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        rel = a.get("rel") or []
        rel_lower = [r.lower() for r in (rel if isinstance(rel, list) else [rel])]
        if "nofollow" in rel_lower:
            nofollow += 1
        # Resolve relative URLs so the host check is consistent
        try:
            absolute = urljoin(base_url, href)
            href_host = urlparse(absolute).netloc.lower()
        except Exception:  # noqa: BLE001
            href_host = ""
        if not href_host or href_host == base_host:
            internal += 1
        else:
            external += 1
        anchor_text = a.get_text(strip=True).lower()
        if anchor_text:
            anchors.add(anchor_text)
    return internal, external, nofollow, len(anchors)


_GA4_RE = re.compile(r"(?:gtag\s*\(|googletagmanager\.com/gtag/js|G-[A-Z0-9]{6,})")
_GTM_RE = re.compile(r"(?:googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]{5,})")
_META_PIXEL_RE = re.compile(r"(?:connect\.facebook\.net|fbq\s*\(|facebook_pixel)")
_LEGACY_GA_RE = re.compile(r"(?:google-analytics\.com/(?:ga|analytics)\.js|UA-\d{4,}-\d+)")


def _extract_analytics(html: str) -> tuple[bool, bool, bool, bool]:
    """Return ``(has_ga4, has_gtm, has_meta_pixel, has_legacy_ga)``.

    Substring scan over the raw HTML — analytics snippets are reliably
    injected by tag managers / CMSes as inline scripts or src URLs. No
    need to parse the script tags individually.
    """
    has_ga4 = bool(_GA4_RE.search(html))
    has_gtm = bool(_GTM_RE.search(html))
    has_meta_pixel = bool(_META_PIXEL_RE.search(html))
    has_legacy_ga = bool(_LEGACY_GA_RE.search(html))
    return has_ga4, has_gtm, has_meta_pixel, has_legacy_ga


def _extract_with_bs4(html: str, base_url: str = "") -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = (title_tag.text or "").strip() if title_tag else ""

    meta_desc = ""
    for m in soup.find_all("meta"):
        if (m.get("name") or "").lower() == "description":
            meta_desc = (m.get("content") or "").strip()
            break

    viewport = ""
    for m in soup.find_all("meta"):
        if (m.get("name") or "").lower() == "viewport":
            viewport = (m.get("content") or "").strip()
            break

    h1s = [h.get_text(strip=True) for h in soup.find_all("h1")]

    has_noindex = False
    for m in soup.find_all("meta"):
        if (m.get("name") or "").lower() == "robots":
            if "noindex" in (m.get("content") or "").lower():
                has_noindex = True
                break

    http_resources: list[str] = []
    for tag in soup.find_all(["img", "script", "link"]):
        src = tag.get("src") or tag.get("href") or ""
        if src.startswith("http://"):
            http_resources.append(src)

    text = soup.get_text(" ", strip=True)

    # Enrichment block (added 2026-05-16) — see module docstring.
    canonical = _extract_canonical(soup)
    og = _extract_open_graph(soup)
    schema_types, has_local_business, has_organization = _extract_schema_org(soup)
    image_count, images_with_alt, images_with_lazy = _extract_image_signals(soup)
    link_internal, link_external, link_nofollow, unique_anchors = _extract_link_graph(
        soup, base_url or "https://example.com",
    )
    has_ga4, has_gtm, has_meta_pixel, has_legacy_ga = _extract_analytics(html)

    return {
        "title": title,
        "meta_description": meta_desc,
        "viewport": viewport,
        "h1_count": len(h1s),
        "h1_text": h1s,
        "has_noindex": has_noindex,
        "http_resources_on_https": http_resources,
        "text": text,
        # --- enrichment ---
        "canonical_url": canonical,
        "og": og,
        "schema_types": schema_types,
        "has_local_business_schema": has_local_business,
        "has_organization_schema": has_organization,
        "image_count": image_count,
        "images_with_alt": images_with_alt,
        "images_with_lazy_load": images_with_lazy,
        "link_internal_count": link_internal,
        "link_external_count": link_external,
        "link_nofollow_count": link_nofollow,
        "unique_anchor_count": unique_anchors,
        "has_ga4": has_ga4,
        "has_gtm": has_gtm,
        "has_meta_pixel": has_meta_pixel,
        "has_legacy_ga": has_legacy_ga,
    }


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta\s+[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_VIEWPORT_RE = re.compile(
    r'<meta\s+[^>]*name=["\']viewport["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_NOINDEX_RE = re.compile(
    r'<meta\s+[^>]*name=["\']robots["\'][^>]*content=["\'][^"\']*noindex',
    re.IGNORECASE,
)
_HTTP_RES_RE = re.compile(
    r'(?:src|href)=["\'](http://[^"\']+)["\']',
    re.IGNORECASE,
)
# Regex fallbacks for the most critical enrichment fields. bs4 is the
# preferred path; these are only used in environments without bs4
# installed. Other enrichment (link graph, image alt-text, schema @types)
# default to empty/zero in the regex path — better than no audit at all.
_CANONICAL_RE = re.compile(
    r'<link\s+[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_RE = re.compile(
    r'<meta\s+[^>]*property=["\']og:([a-z_]+)["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _extract_with_regex(html: str) -> dict[str, Any]:
    title = ""
    m = _TITLE_RE.search(html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()

    meta_desc = ""
    m = _META_DESC_RE.search(html)
    if m:
        meta_desc = m.group(1).strip()

    viewport = ""
    m = _VIEWPORT_RE.search(html)
    if m:
        viewport = m.group(1).strip()

    h1s = [re.sub(r"<[^>]+>", "", h).strip() for h in _H1_RE.findall(html)]
    has_noindex = bool(_NOINDEX_RE.search(html))
    http_resources = _HTTP_RES_RE.findall(html)
    text = re.sub(r"<[^>]+>", " ", html)

    # Minimal regex fallback for the highest-leverage enrichment signals.
    # Anything more structural (link graph, alt-text coverage) defaults to
    # empty / zero — those checks will be skipped under the regex path.
    canonical = ""
    m = _CANONICAL_RE.search(html)
    if m:
        canonical = m.group(1).strip()

    og: dict[str, str] = {}
    for key, value in _OG_RE.findall(html):
        og[key.lower()] = value.strip()

    schema_types: list[str] = []
    has_local_business = False
    has_organization = False
    for raw_block in _JSON_LD_RE.findall(html):
        try:
            parsed = _json.loads(raw_block.strip())
        except (ValueError, TypeError):
            continue
        candidates: list[dict] = []
        if isinstance(parsed, dict):
            graph = parsed.get("@graph")
            if isinstance(graph, list):
                candidates.extend(x for x in graph if isinstance(x, dict))
            else:
                candidates.append(parsed)
        elif isinstance(parsed, list):
            candidates.extend(x for x in parsed if isinstance(x, dict))
        for node in candidates:
            t = node.get("@type")
            t_list = t if isinstance(t, list) else [t] if t else []
            for raw_type in t_list:
                if not isinstance(raw_type, str):
                    continue
                clean = raw_type.rsplit("/", 1)[-1].strip()
                if not clean:
                    continue
                schema_types.append(clean)
                if "LocalBusiness" in clean or clean in (
                    "Plumber", "Electrician", "Restaurant", "Dentist",
                    "RoofingContractor", "HVACBusiness",
                ):
                    has_local_business = True
                if clean in ("Organization", "LocalBusiness"):
                    has_organization = True

    has_ga4, has_gtm, has_meta_pixel, has_legacy_ga = _extract_analytics(html)

    return {
        "title": title,
        "meta_description": meta_desc,
        "viewport": viewport,
        "h1_count": len(h1s),
        "h1_text": h1s,
        "has_noindex": has_noindex,
        "http_resources_on_https": http_resources,
        "text": text,
        # --- enrichment (minimal regex fallback) ---
        "canonical_url": canonical,
        "og": og,
        "schema_types": schema_types,
        "has_local_business_schema": has_local_business,
        "has_organization_schema": has_organization,
        # Structural extractors (alt-text, link graph) only run under bs4.
        # Defaults are 0/[] so _build_issues skips their checks gracefully.
        "image_count": 0,
        "images_with_alt": 0,
        "images_with_lazy_load": 0,
        "link_internal_count": 0,
        "link_external_count": 0,
        "link_nofollow_count": 0,
        "unique_anchor_count": 0,
        "has_ga4": has_ga4,
        "has_gtm": has_gtm,
        "has_meta_pixel": has_meta_pixel,
        "has_legacy_ga": has_legacy_ga,
    }


def _build_issues(url: str, parsed: dict[str, Any]) -> list[SeoIssue]:
    issues: list[SeoIssue] = []
    title = parsed.get("title") or ""
    if not title:
        issues.append(SeoIssue(
            id="missing_title", severity="high", category="content",
            message="Page is missing a <title> tag.", evidence=""))
    elif len(title) < 10 or len(title) > 70:
        issues.append(SeoIssue(
            id="title_length", severity="medium", category="content",
            message=f"Title length {len(title)} chars (recommended 10-70).",
            evidence=title[:120]))

    meta = parsed.get("meta_description") or ""
    if not meta:
        issues.append(SeoIssue(
            id="missing_meta_description", severity="high", category="content",
            message="Page is missing a meta description.", evidence=""))
    elif len(meta) < 50 or len(meta) > 160:
        issues.append(SeoIssue(
            id="meta_description_length", severity="medium", category="content",
            message=f"Meta description length {len(meta)} chars (recommended 50-160).",
            evidence=meta[:160]))

    h1_count = parsed.get("h1_count") or 0
    if h1_count == 0:
        issues.append(SeoIssue(
            id="missing_h1", severity="high", category="content",
            message="Page has no <h1> heading.", evidence=""))
    elif h1_count > 1:
        issues.append(SeoIssue(
            id="multiple_h1", severity="medium", category="content",
            message=f"Page has {h1_count} <h1> headings; only one is recommended.",
            evidence=""))

    if not parsed.get("viewport"):
        issues.append(SeoIssue(
            id="missing_viewport_meta", severity="high", category="mobile",
            message="Mobile viewport meta tag is missing.", evidence=""))

    # NAP (Name / Address / Phone) signals. Skipped if the page already
    # publishes structured LocalBusiness data — the schema fields ARE the
    # NAP-machine-readable record + cover the same ground for crawlers.
    # Phone regex extended to handle international + parens + dot/space
    # separators; address regex catches a wider keyword set.
    if not parsed.get("has_local_business_schema"):
        text = (parsed.get("text") or "").lower()
        phone_regex = re.compile(
            r"\b\+?\d{1,3}[\s.\-]?\(?\d{2,4}\)?[\s.\-]?\d{2,4}[\s.\-]?\d{2,4}\b",
        )
        address_regex = re.compile(
            r"\b(address|street|avenue|road|boulevard|highway|drive|lane|court|"
            r"plaza|parkway|suite|unit|ste\.?|st\.|ave\.?|rd\.?|blvd\.?|hwy\.?|"
            r"dr\.?|ln\.?|ct\.?|pkwy\.?)\b",
        )
        zip_regex = re.compile(r"\b\d{5}(?:-\d{4})?\b")  # US ZIP / ZIP+4
        has_phone = bool(phone_regex.search(text))
        has_address_keyword = bool(address_regex.search(text))
        has_zip = bool(zip_regex.search(text))
        if not has_phone and not (has_address_keyword or has_zip):
            issues.append(SeoIssue(
                id="missing_local_signals", severity="medium", category="local",
                message=(
                    "Page has no visible phone number or street-address tokens. "
                    "Local-pack ranking + Maps listings depend on the NAP "
                    "(Name / Address / Phone) being readable on the page."
                ),
                evidence=""))

    if urlparse(url).scheme == "https" and parsed.get("http_resources_on_https"):
        sample = parsed["http_resources_on_https"][:3]
        issues.append(SeoIssue(
            id="mixed_content", severity="critical", category="technical",
            message="HTTPS page loads HTTP sub-resources (mixed content).",
            evidence=", ".join(sample)))

    if parsed.get("has_noindex"):
        issues.append(SeoIssue(
            id="noindex_directive", severity="critical", category="technical",
            message="Page explicitly opts out of indexing (noindex).",
            evidence=""))

    # --- Enrichment-driven issues (added 2026-05-16) --------------------
    _build_enrichment_issues(url, parsed, issues)

    return issues


def _build_pagespeed_issues(
    ps: PageSpeedResult, issues: list[SeoIssue],
) -> None:
    """Append PageSpeed Insights-driven issues in place.

    Only fires when the PSI call succeeded (``ps.error`` empty). Thresholds
    mirror Google's published Lighthouse cutoffs:
      Performance score: >=90 good, 50-89 needs-improvement (MEDIUM),
                         <50 poor (HIGH)
      LCP:               <=2500ms good, 2500-4000ms NI (MEDIUM),
                         >4000ms poor (HIGH)
      CLS:               <=0.1 good, 0.1-0.25 NI (MEDIUM),
                         >0.25 poor (HIGH)
    The needs-improvement tier was added 2026-05-16 — earlier cuts only
    scored 'poor.' NI is medium because the prospect probably won't
    notice it day-to-day but it's costing them ranking + UX headroom.
    """
    if ps.error:
        # PSI didn't return scores (api_key_unset, transport error, quota,
        # etc.) — surface as informational, not as a scored issue.
        return

    # ---- Performance score ---------------------------------------------
    if ps.performance_score is not None:
        if ps.performance_score < 50:
            issues.append(SeoIssue(
                id="pagespeed_performance_poor", severity="high",
                category="technical",
                message=(
                    f"Google PageSpeed gives this page a Performance score of "
                    f"{ps.performance_score}/100 on mobile. Below 50 is the "
                    "'poor' tier — slow page speed directly suppresses ranking "
                    "and increases bounce rate."
                ),
                evidence=f"strategy={ps.strategy}",
            ))
        elif ps.performance_score < 90:
            issues.append(SeoIssue(
                id="pagespeed_performance_needs_improvement",
                severity="medium", category="technical",
                message=(
                    f"PageSpeed Performance is {ps.performance_score}/100 on "
                    "mobile — Google's 'needs improvement' tier (50-89). "
                    "Not actively penalized, but you're leaving ranking + "
                    "user-experience headroom on the table."
                ),
                evidence=f"strategy={ps.strategy}",
            ))

    # ---- Largest Contentful Paint --------------------------------------
    if ps.lcp_ms is not None:
        if ps.lcp_ms > 4000:
            issues.append(SeoIssue(
                id="pagespeed_lcp_poor", severity="high", category="technical",
                message=(
                    f"Largest Contentful Paint is {ps.lcp_ms} ms — Google "
                    "treats anything above 4000 ms as 'poor.' LCP is a Core "
                    "Web Vital and a direct ranking factor."
                ),
                evidence="threshold: <2500ms good, >4000ms poor",
            ))
        elif ps.lcp_ms > 2500:
            issues.append(SeoIssue(
                id="pagespeed_lcp_needs_improvement",
                severity="medium", category="technical",
                message=(
                    f"LCP is {ps.lcp_ms} ms — Google's 'needs improvement' "
                    "band (2500-4000ms). Not yet in the penalty zone, but "
                    "every 100ms above 2500ms costs measurable conversion."
                ),
                evidence="threshold: <2500ms good, 2500-4000ms NI, >4000ms poor",
            ))

    # ---- Cumulative Layout Shift ---------------------------------------
    if ps.cls is not None:
        if ps.cls > 0.25:
            issues.append(SeoIssue(
                id="pagespeed_cls_poor", severity="high", category="technical",
                message=(
                    f"Cumulative Layout Shift is {ps.cls} — Google treats "
                    "anything above 0.25 as 'poor.' CLS is a Core Web Vital "
                    "and a direct ranking factor; users also abandon pages "
                    "that shift under their tap."
                ),
                evidence="threshold: <0.1 good, >0.25 poor",
            ))
        elif ps.cls > 0.1:
            issues.append(SeoIssue(
                id="pagespeed_cls_needs_improvement",
                severity="medium", category="technical",
                message=(
                    f"CLS is {ps.cls} — Google's 'needs improvement' band "
                    "(0.1-0.25). Page content is shifting noticeably as it "
                    "loads. Not yet penalized, but worth fixing for tap "
                    "accuracy + user trust."
                ),
                evidence="threshold: <0.1 good, 0.1-0.25 NI, >0.25 poor",
            ))


def _build_enrichment_issues(
    url: str, parsed: dict[str, Any], issues: list[SeoIssue],
) -> None:
    """Append enrichment-driven issues in place. Pulled out of
    ``_build_issues`` so the original checklist stays readable.
    """
    # ---- Canonical -----------------------------------------------------
    canonical = (parsed.get("canonical_url") or "").strip()
    if not canonical:
        issues.append(SeoIssue(
            id="missing_canonical", severity="high", category="technical",
            message=(
                "Page has no canonical URL tag. Without it, Google can treat "
                "HTTPS/HTTP, www/non-www, and trailing-slash variants as "
                "duplicates and split your ranking power across them."
            ),
            evidence="",
        ))

    # ---- Open Graph (social previews) ----------------------------------
    og = parsed.get("og") or {}
    if not og.get("title"):
        issues.append(SeoIssue(
            id="missing_og_title", severity="medium", category="content",
            message=(
                "No Open Graph title set. When this page is shared on "
                "Facebook, LinkedIn, or messaging apps, the preview will "
                "fall back to the page title or be blank."
            ),
            evidence="",
        ))
    if not og.get("image"):
        issues.append(SeoIssue(
            id="missing_og_image", severity="medium", category="content",
            message=(
                "No Open Graph image set. Social shares of this URL will "
                "appear without a thumbnail — significantly lower click-through."
            ),
            evidence="",
        ))

    # ---- schema.org JSON-LD --------------------------------------------
    schema_types = parsed.get("schema_types") or []
    has_local_business = bool(parsed.get("has_local_business_schema"))
    has_organization = bool(parsed.get("has_organization_schema"))
    if not schema_types:
        issues.append(SeoIssue(
            id="missing_schema_org", severity="high", category="technical",
            message=(
                "No schema.org structured data (JSON-LD) found. This is the "
                "primary mechanism Google uses to understand what your "
                "business is and where you operate."
            ),
            evidence="",
        ))
    elif not (has_local_business or has_organization):
        issues.append(SeoIssue(
            id="missing_local_business_schema", severity="high", category="local",
            message=(
                "Structured data is present, but no LocalBusiness or "
                "Organization @type is declared. Local search and Google "
                "Maps rely on this to display your name, address, phone, "
                "and hours in search results."
            ),
            evidence=f"types found: {', '.join(schema_types[:5])}",
        ))

    # ---- Alt-text coverage (bs4-only — regex path reports 0/0) ---------
    image_count = int(parsed.get("image_count") or 0)
    images_with_alt = int(parsed.get("images_with_alt") or 0)
    if image_count > 0:
        coverage = images_with_alt / image_count
        if coverage < 0.5:
            issues.append(SeoIssue(
                id="low_alt_text_coverage_critical", severity="high",
                category="content",
                message=(
                    f"Only {images_with_alt} of {image_count} images "
                    f"({coverage * 100:.0f}%) have alt text. This blocks "
                    "image search ranking and breaks the page for users on "
                    "screen readers."
                ),
                evidence="",
            ))
        elif coverage < 0.8:
            issues.append(SeoIssue(
                id="low_alt_text_coverage", severity="medium",
                category="content",
                message=(
                    f"{images_with_alt} of {image_count} images "
                    f"({coverage * 100:.0f}%) have alt text. Recommended is "
                    "80% or higher for accessibility + image search."
                ),
                evidence="",
            ))

    # ---- Link graph (bs4-only) -----------------------------------------
    link_internal = int(parsed.get("link_internal_count") or 0)
    link_external = int(parsed.get("link_external_count") or 0)
    # Only flag when the bs4 path actually ran (image_count is a useful
    # proxy — regex path reports 0 even if the page has images).
    bs4_ran = parsed.get("image_count", 0) > 0 or link_internal + link_external > 0
    if bs4_ran and link_internal == 0:
        issues.append(SeoIssue(
            id="no_internal_links", severity="high", category="technical",
            message=(
                "Page has no internal links to other pages on your site. "
                "Search engines use these to discover and rank your content; "
                "an orphan page is invisible to crawlers reaching it from "
                "elsewhere."
            ),
            evidence="",
        ))

    # ---- Analytics (HTML-string scan — runs under both parsers) --------
    has_ga4 = bool(parsed.get("has_ga4"))
    has_gtm = bool(parsed.get("has_gtm"))
    has_meta_pixel = bool(parsed.get("has_meta_pixel"))
    has_legacy_ga = bool(parsed.get("has_legacy_ga"))
    if not (has_ga4 or has_gtm or has_meta_pixel or has_legacy_ga):
        issues.append(SeoIssue(
            id="no_analytics_detected", severity="high", category="technical",
            message=(
                "No analytics tracking detected (Google Analytics 4, Google "
                "Tag Manager, or Meta Pixel). Without it, you can't measure "
                "which marketing efforts actually bring in customers — every "
                "optimization is a guess."
            ),
            evidence="",
        ))
    elif has_legacy_ga and not (has_ga4 or has_gtm):
        issues.append(SeoIssue(
            id="legacy_analytics_only", severity="medium", category="technical",
            message=(
                "Only the legacy Google Analytics (Universal Analytics) is "
                "installed. UA stopped processing new data on July 1, 2023 — "
                "the dashboard shows nothing current. Upgrade to GA4."
            ),
            evidence="",
        ))


def _score(issues: list[SeoIssue]) -> int:
    penalty = 0
    for i in issues:
        if i.severity == "critical":
            penalty += 5
        elif i.severity == "high":
            penalty += 3
        elif i.severity == "medium":
            penalty += 1
    return max(0, min(100, 100 - penalty))


def audit_url(url: str, keywords: list[str] | None = None, industry: str = "") -> AuditResult:
    """Run the deterministic audit and return a populated :class:`AuditResult`."""
    keywords = keywords or []
    # Check robots.txt BEFORE the page fetch so the result reflects what a
    # well-behaved crawler (Googlebot) would see. We still fetch + audit
    # disallowed pages — the operator/prospect consented by requesting the
    # audit — but we emit a CRITICAL issue so the prospect sees that their
    # robots.txt is the reason they're invisible to search engines.
    robots_status, robots_allows = _check_robots_txt(url)

    status, html, headers = _fetch(url)

    if status == 0 or not html:
        # G6: fetch_failed is sourced deterministically from the HTTP
        # transport layer (non-2xx / connection failure) — tag it
        # http_status so it survives the Gap Report serialization filter.
        issue = SeoIssue(
            id="fetch_failed", severity="critical", category="technical",
            message=f"Could not fetch page (status={status}).", evidence="",
            evidence_source="http_status")
        return AuditResult(
            url=url, seo_score=0, issues=[issue],
            findings={"status_code": status, "fetched": False, "industry": industry,
                      "keywords": keywords, "robots_txt_status": robots_status,
                      "robots_txt_allows": robots_allows},
            evidence_sources={"fetch_failed": "http_status"},
            ts=iso_now(),
        )

    parsed = _extract_with_bs4(html, url) if _HAS_BS4 else _extract_with_regex(html)
    issues = _build_issues(url, parsed)
    # robots.txt-driven issue, layered on after the page-content checks
    # so the robots issue surfaces in the report's Technical section.
    if not robots_allows:
        # G6: sourced from robots.txt parse.
        issues.append(SeoIssue(
            id="blocked_by_robots", severity="critical", category="technical",
            message=(
                "Your robots.txt is blocking crawlers from indexing this page. "
                "This is almost certainly the reason it doesn't show up in "
                "Google searches. Audit ran with operator consent; "
                "production search engines will respect the Disallow."
            ),
            evidence=_robots_url(url),
            evidence_source="robots_txt",
        ))

    # PageSpeed Insights — layered on after on-page extraction so the
    # external API call is the slowest thing in the audit and can fail
    # without invalidating any of the deterministic findings. When
    # GOOGLE_PAGESPEED_API_KEY is unset, both calls return
    # error='api_key_unset' and _build_pagespeed_issues no-ops; the audit
    # ships with the local checks alone.
    #
    # Mobile runs first because mobile-first is the rank-affecting view
    # for local-services prospects. Only mobile drives scored issues —
    # desktop scores are informational data in findings to avoid double-
    # penalizing for the same physics.
    pagespeed = audit_pagespeed(url, strategy="mobile")
    _build_pagespeed_issues(pagespeed, issues)
    pagespeed_desktop = audit_pagespeed(url, strategy="desktop")

    # Passive security & trust-posture audit (2026-05-20). Wired here, not in
    # service.py, because audit_url already owns the homepage fetch: the
    # security audit reuses the homepage `headers` + `html` + http-resource
    # list with zero re-fetches, and lands its result inside
    # AuditResult.findings["security"] so write_seo_report is fed
    # automatically with no AuditResult schema change. Strictly passive +
    # zero-LLM; gated by the seo_security_audit_enabled toggle and wrapped so
    # any failure degrades to an omitted section, never sinking the audit.
    security_result: dict[str, Any] = {}
    try:
        if get_settings().seo_security_audit_enabled:
            security_result = audit_security(
                url,
                headers=headers,
                html=html,
                http_resources=parsed.get("http_resources_on_https", []),
            )
    except Exception as exc:  # noqa: BLE001 - security audit never blocks SEO
        _LOG.warning("security audit failed url=%s err=%s", url, exc)
        security_result = {}

    image_count = parsed.get("image_count", 0) or 0
    images_with_alt = parsed.get("images_with_alt", 0) or 0
    alt_coverage_pct = (
        round(100.0 * images_with_alt / image_count, 1)
        if image_count > 0 else None
    )
    findings = {
        "status_code": status,
        "fetched": True,
        "title": parsed.get("title", ""),
        "meta_description": parsed.get("meta_description", ""),
        "viewport": parsed.get("viewport", ""),
        "h1_count": parsed.get("h1_count", 0),
        "h1_text": parsed.get("h1_text", []),
        "has_noindex": parsed.get("has_noindex", False),
        "mixed_content_count": len(parsed.get("http_resources_on_https", [])),
        "industry": industry,
        "keywords": keywords,
        "parser": "bs4" if _HAS_BS4 else "regex",
        # robots.txt (accuracy cut, 2026-05-16)
        "robots_txt_status": robots_status,
        "robots_txt_allows": robots_allows,
        # PageSpeed Insights (Cut C, 2026-05-16). All None / "" when the
        # API key is unset; operator sees error='api_key_unset' to know to
        # provision GOOGLE_PAGESPEED_API_KEY.
        # Mobile = scored (mobile-first ranking). Desktop = informational.
        "pagespeed_strategy": pagespeed.strategy,
        "pagespeed_performance_score": pagespeed.performance_score,
        "pagespeed_accessibility_score": pagespeed.accessibility_score,
        "pagespeed_seo_score": pagespeed.seo_score,
        "pagespeed_best_practices_score": pagespeed.best_practices_score,
        "pagespeed_lcp_ms": pagespeed.lcp_ms,
        "pagespeed_cls": pagespeed.cls,
        "pagespeed_tbt_ms": pagespeed.tbt_ms,
        "pagespeed_fcp_ms": pagespeed.fcp_ms,
        "pagespeed_speed_index_ms": pagespeed.speed_index_ms,
        "pagespeed_error": pagespeed.error,
        # Desktop scores — informational. Surface so operator can compare
        # mobile vs desktop deltas (e.g. a site with great desktop but
        # terrible mobile suggests a missing responsive layout). Not used
        # to drive scored issues; mobile-first is what Google ranks on.
        "pagespeed_desktop_performance_score": pagespeed_desktop.performance_score,
        "pagespeed_desktop_accessibility_score": pagespeed_desktop.accessibility_score,
        "pagespeed_desktop_seo_score": pagespeed_desktop.seo_score,
        "pagespeed_desktop_best_practices_score": pagespeed_desktop.best_practices_score,
        "pagespeed_desktop_lcp_ms": pagespeed_desktop.lcp_ms,
        "pagespeed_desktop_cls": pagespeed_desktop.cls,
        "pagespeed_desktop_tbt_ms": pagespeed_desktop.tbt_ms,
        "pagespeed_desktop_fcp_ms": pagespeed_desktop.fcp_ms,
        "pagespeed_desktop_speed_index_ms": pagespeed_desktop.speed_index_ms,
        "pagespeed_desktop_error": pagespeed_desktop.error,
        # --- enrichment (2026-05-16) ---
        "canonical_url": parsed.get("canonical_url", ""),
        "og": parsed.get("og", {}),
        "schema_types": parsed.get("schema_types", []),
        "has_local_business_schema": parsed.get("has_local_business_schema", False),
        "has_organization_schema": parsed.get("has_organization_schema", False),
        "image_count": image_count,
        "images_with_alt": images_with_alt,
        "alt_coverage_pct": alt_coverage_pct,
        "images_with_lazy_load": parsed.get("images_with_lazy_load", 0),
        "link_internal_count": parsed.get("link_internal_count", 0),
        "link_external_count": parsed.get("link_external_count", 0),
        "link_nofollow_count": parsed.get("link_nofollow_count", 0),
        "unique_anchor_count": parsed.get("unique_anchor_count", 0),
        "has_ga4": parsed.get("has_ga4", False),
        "has_gtm": parsed.get("has_gtm", False),
        "has_meta_pixel": parsed.get("has_meta_pixel", False),
        "has_legacy_ga": parsed.get("has_legacy_ga", False),
    }
    # Passive security audit (2026-05-20). Only attached when the toggle is
    # on AND the audit produced a result; an empty dict means the section is
    # omitted from the customer-facing report.
    if security_result:
        findings["security"] = security_result
    # G6: derive the audit-level evidence_sources map from any issue whose
    # emitter tagged a deterministic source. Used by the Codex validator
    # (chapter 12 / ADR-009) + the Gap Report serialization filter.
    evidence_sources = {
        i.id: i.evidence_source for i in issues if i.evidence_source is not None
    }
    return AuditResult(
        url=url, seo_score=_score(issues), issues=issues, findings=findings,
        evidence_sources=evidence_sources,
        ts=iso_now(),
    )
