"""Minimal homepage fetcher for prospecting SEO enrichment.

Single-page GET via ``httpx.Client``, dead/junk classification via URL
heuristics. The signal is intentionally coarse — the seo_audit module does the
content-shape scoring on top of the html returned here.

Two hardening measures keep a healthy site from being mis-classified as a
broken one (the 2026-05-21 false-positive sweep — bot-blocked dealer/realty
sites were flagged "WEBSITE BROKEN" on the operator call list):

  * **Browser User-Agent.** A self-identifying ``HustleForgeProspectingBot``
    UA was rejected outright (HTTP 403) by common WAFs; the fetcher now sends
    a mainstream desktop-Chrome UA + the headers a browser sends, which gets
    past UA-only blocks.
  * **One retry on a non-terminal result.** A single transient timeout / 5xx /
    rate-limit at crawl time used to label a site dead for the whole day;
    ``fetch_homepage`` now retries once (short backoff) before giving up.

A JS-challenge WAF still blocks a headless GET — that is reported honestly as
the ``access_blocked`` status (the site loads fine in a real browser, so it is
NOT a cold-call "your site is down" hook), distinct from a genuinely broken
``http_error`` / ``server_error`` page or a permanently-removed ``gone`` one.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from backend.common.safe_fetch import (
    BROWSER_HEADERS,
    HTTP2_AVAILABLE,
    SsrfBlockedError,
    assert_public_http_url,
    bounded_timeout,
)
from backend.common.shared_http import get_shared_client

_LOG = logging.getLogger("samus.prospecting.crawler")

# Phase-split transport budget (2026-07-01 hang fix). A scalar 10s is defeated
# by a slow-trickle read — the exact stalled Facebook ``:443`` read that hung
# the daily run. Cap connect/read/write/pool independently so a wedged homepage
# fetch can never hold the sequential run open. ``_MAX_REDIRECTS`` bounds the
# redirect chain so ``follow_redirects=True`` cannot multiply the wall clock.
_TIMEOUT = bounded_timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

# Redirect-chain ceiling. httpx defaults to 20; a hostile origin can chain a
# long redirect train, each hop re-spending the read budget, to multiply the
# wall clock. A small cap keeps the total fetch bounded.
_MAX_REDIRECTS = 5

# Retry budget for a non-terminal fetch result (timeout / connection error /
# 5xx / 403 / 429). A definitive 2xx/3xx or a 404/410 is never retried.
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SEC = 1.5

# Browser-equivalent request identity (shared with the SEO audit fetchers).
# The prior self-identifying bot UA tripped WAF UA blocklists (Cloudflare,
# dealer-platform firewalls), which 403'd the fetch and mis-flagged a healthy
# site as broken — see backend.common.safe_fetch.BROWSER_HEADERS.
_DEFAULT_HEADERS = BROWSER_HEADERS

# Domains whose websites are not the prospect's actual site.
_SOCIAL_ONLY_HOSTS = (
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "yelp.com",
    "www.yelp.com",
    "yellowpages.com",
    "www.yellowpages.com",
    "linkedin.com",
    "www.linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
)

# Hosts/markers used by domain-parking services.
_PARKED_HOSTS = (
    "sedoparking.com",
    "godaddy.com/domains",
    "namecheap.com/domains",
)
_PARKED_HTML_MARKERS = (
    "domain is for sale",
    "buy this domain",
    "parked free",
    "this domain is parked",
)


def _host_of(url: str) -> str:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return ""
    return (parsed.host or "").lower()


def _fetch_once(url: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """One GET attempt. Never raises — transport errors land in ``fetch_error``.

    ``client`` may be supplied by the caller (tests / explicit injection);
    when None we reuse a process-wide keep-alive client matching this
    module's request profile (see ``shared_http.get_shared_client``).
    """
    # RT NET-01: the URL is attacker-influenceable (a poisoned Google Places
    # websiteUri / contact-validation input). Block it resolving to a non-public
    # address (IMDS 169.254.169.254, loopback, RFC-1918) before we ever connect.
    try:
        assert_public_http_url(url)
    except SsrfBlockedError as exc:
        return {"final_url": url, "status_code": 0, "html": None, "fetch_error": f"ssrf_blocked: {exc}"}
    if client is None:
        client = get_shared_client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=dict(_DEFAULT_HEADERS),
            http2=HTTP2_AVAILABLE,
            max_redirects=_MAX_REDIRECTS,
        )
    try:
        response = client.get(url)
    except httpx.TimeoutException as exc:
        return {"final_url": url, "status_code": 0, "html": None, "fetch_error": f"timeout: {exc}"}
    except httpx.HTTPError as exc:
        return {"final_url": url, "status_code": 0, "html": None, "fetch_error": f"{exc.__class__.__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - log any unexpected condition
        _LOG.warning("fetch_homepage unexpected error url=%s err=%s", url, exc)
        return {"final_url": url, "status_code": 0, "html": None, "fetch_error": f"unexpected: {exc}"}

    status = response.status_code
    final_url = str(response.url)
    if status >= 400:
        return {"final_url": final_url, "status_code": status, "html": None, "fetch_error": None}

    text = response.text or ""
    return {"final_url": final_url, "status_code": status, "html": text, "fetch_error": None}


def _is_terminal(status: int) -> bool:
    """True when a fetch result is not worth retrying.

    A 2xx/3xx is a real answer; a 404/410 is the server's definitive "this
    resource is not (or no longer) here" — it will keep returning it. Anything
    else (status 0 transport failure, 403/429 throttle, 5xx) may be transient.
    """
    return (200 <= status < 400) or status in (404, 410)


def fetch_homepage(url: str) -> dict[str, Any]:
    """Fetch ``url`` and return ``{final_url, status_code, html, fetch_error}``.

    Retries once on a non-terminal result (transient timeout / 5xx / throttle)
    so a single crawl-time blip cannot mislabel a healthy site for the day.
    Never raises — transport errors are captured in ``fetch_error``.
    """
    if not url or not isinstance(url, str):
        return {"final_url": url or "", "status_code": 0, "html": None, "fetch_error": "empty_url"}

    result = _fetch_once(url)
    attempts = 1
    while attempts < _MAX_ATTEMPTS and not _is_terminal(int(result.get("status_code") or 0)):
        _LOG.info(
            "fetch_homepage retry url=%s attempt=%d prior_status=%s",
            url, attempts + 1, result.get("status_code"),
        )
        time.sleep(_RETRY_BACKOFF_SEC)
        result = _fetch_once(url)
        attempts += 1
    return result


# Reachability classes returned by classify_website. Anything other than
# "live" means the prospect has no usable website — which is itself a strong
# cold-call hook (see text_export's website-alert rendering) — EXCEPT
# "access_blocked", where the site loads fine for a human and only a WAF
# stopped our headless fetch (not a "your site is down" hook).
WEBSITE_STATUSES: tuple[str, ...] = (
    "live",                # fetched OK, real first-party content
    "domain_unresolved",   # DNS lookup failed — the domain doesn't exist
    "unreachable_timeout", # connected attempt timed out
    "unreachable",         # other connection failure
    "server_error",        # HTTP 5xx
    "access_blocked",      # HTTP 403/429 — a WAF blocked the crawl; site is
                           # almost certainly fine for a real visitor
    "gone",                # HTTP 410 — the page is permanently removed
    "http_error",          # other HTTP 4xx (e.g. 404)
    "empty",               # 2xx but no html body
    "parked",              # domain-parking placeholder
    "social_only",         # "website" is really a facebook/yelp/etc page
)


def classify_website(page: dict[str, Any]) -> str:
    """Classify a crawled page into one of ``WEBSITE_STATUSES``.

    A finer-grained read of the same heuristics ``is_dead_or_junk`` uses, so the
    call list can tell the operator *why* a site is unusable — a non-resolving
    domain is a different (and more pitchable) conversation than a 404, and a
    WAF-blocked crawl (``access_blocked``) is not a broken site at all.
    """
    if not isinstance(page, dict):
        return "unreachable"

    status = int(page.get("status_code") or 0)
    err = (page.get("fetch_error") or "").lower()

    if status == 0:
        if ("getaddrinfo" in err or "name or service not known" in err
                or "nodename nor servname" in err
                or "no address associated" in err):
            return "domain_unresolved"
        if "timeout" in err or "timed out" in err:
            return "unreachable_timeout"
        return "unreachable"
    if status >= 500:
        return "server_error"
    if status in (403, 429):
        # A WAF / rate-limiter refused the headless crawl. The site itself is
        # almost certainly serving real visitors fine — do NOT treat this as a
        # broken-website cold-call hook.
        return "access_blocked"
    if status == 410:
        return "gone"
    if status >= 400:
        return "http_error"

    html = page.get("html")
    if not html or not isinstance(html, str) or not html.strip():
        return "empty"

    host = _host_of(str(page.get("final_url") or ""))
    if host in _SOCIAL_ONLY_HOSTS:
        return "social_only"
    if any(parked in host for parked in _PARKED_HOSTS):
        return "parked"
    if any(marker in html.lower() for marker in _PARKED_HTML_MARKERS):
        return "parked"
    return "live"


def is_dead_or_junk(page: dict[str, Any]) -> bool:
    """True if the crawled page should not be treated as the prospect's site.

    Thin predicate over :func:`classify_website` — anything but ``"live"`` is
    dead or junk (non-2xx/3xx, empty html, social-only host, parked domain).
    Note ``access_blocked`` is dead *to the crawler* (we could not fetch it),
    even though such a site is typically healthy for a human visitor.
    """
    return classify_website(page) != "live"
