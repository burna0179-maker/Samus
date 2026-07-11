"""Google PageSpeed Insights API client + thin Pydantic wrapper.

PageSpeed Insights runs a Lighthouse audit from Google's infrastructure
and returns Performance / Accessibility / SEO / Best-Practices subscores
plus the underlying Core Web Vitals: LCP, CLS, TBT (lab proxy for INP),
FCP, Speed Index. This module wraps the v5 REST API so the audit workcell
can layer real performance metrics on top of its on-page extraction —
something httpx+bs4 cannot do because CWV require a real headless render.

Local-first / degraded-mode contract:
  - When ``GOOGLE_PAGESPEED_API_KEY`` is unset, ``audit_pagespeed`` returns
    a ``PageSpeedResult`` with ``error="api_key_unset"`` and every score
    field set to None. Callers must tolerate this — the audit still ships
    using the on-page checks alone.
  - When PSI returns a 5xx or transport error, the result carries
    ``error=...`` and continues; this is informational data, not blocking.
  - Quota: PSI free tier is 25,000 req/day. We make 1 request per audit
    so the seo workcell cannot drain quota on its own.

Why a dedicated module rather than extending stripe/vapi-style clients:
  - PSI has its own pagination + audits dict shape worth keeping isolated
  - The audit module already has its own httpx singleton; this module's
    fetch path is monkeypatched independently in tests
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from backend.common import safe_fetch


_LOG = logging.getLogger("samus.seo.pagespeed")

# Module-local keep-alive client. Mirrors the pattern in ``seo.audit``: the
# cache key is the *current* ``httpx.Client`` identity read off THIS
# module's ``httpx`` attribute, not the real ``httpx`` singleton. The
# PageSpeed test suite (test_seo_pagespeed.py) monkeypatches
# ``pagespeed_client.httpx`` with a ``_FakeHttpx`` whose ``.Client`` is a
# stub; the identity check below invalidates the cached real-httpx client
# and a fresh stub is built — preserving per-test isolation. Production
# amortizes one TCP+TLS handshake across all PSI calls in a batch.
_SHARED_CLIENT: httpx.Client | None = None
_SHARED_CLIENT_CLASS: type | None = None


def _get_shared_client() -> httpx.Client:
    global _SHARED_CLIENT, _SHARED_CLIENT_CLASS
    Client = httpx.Client
    if _SHARED_CLIENT is None or _SHARED_CLIENT_CLASS is not Client:
        prev = _SHARED_CLIENT
        if prev is not None:
            try:
                prev.close()
            except Exception:  # noqa: BLE001 — rotation never raises
                pass
        _SHARED_CLIENT = Client(
            timeout=_PSI_TIMEOUT, follow_redirects=True, max_redirects=3,
        )
        _SHARED_CLIENT_CLASS = Client
    return _SHARED_CLIENT

_PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
# Phase-split budget (2026-07-01 hang fix). A scalar 30s is defeated by a
# slow-trickle read (a server that ACCEPTs then dribbles bytes resets the
# scalar read deadline on every byte and hangs the worker indefinitely). The
# read window is generous — Lighthouse on Google's side typically takes 10-25s
# — but connect/write/pool are capped tight so a wedged PSI endpoint cannot
# stall the audit past its own deadline. ``max_redirects`` (above) bounds the
# googleapis→CDN redirect chain so ``follow_redirects=True`` can't multiply the
# wall clock.
_PSI_TIMEOUT: httpx.Timeout = safe_fetch.bounded_timeout(
    connect=5.0, read=30.0, write=10.0, pool=5.0,
)
_DEFAULT_STRATEGY = "mobile"  # mobile-first; mobile rankings dominate locally

_USER_AGENT = "samus-seo-audit/1.0 (+https://hustleforge.tech)"


class PageSpeedResult(BaseModel):
    """Subset of the PageSpeed Insights v5 response Samus actually uses.

    Scores are 0-100 (Lighthouse convention) when present, None when the
    API was unreachable / unauthorized. CWV are reported as the metrics
    Google publishes for ranking — LCP / CLS / TBT (lab proxy for INP).
    """
    model_config = ConfigDict(extra="forbid")

    strategy: str = _DEFAULT_STRATEGY    # 'mobile' or 'desktop'
    performance_score: int | None = None
    accessibility_score: int | None = None
    seo_score: int | None = None
    best_practices_score: int | None = None
    # Core Web Vitals — milliseconds for LCP/TBT/FCP, unitless for CLS.
    lcp_ms: int | None = None
    cls: float | None = None
    tbt_ms: int | None = None
    fcp_ms: int | None = None
    speed_index_ms: int | None = None
    # When set, scoring + issues callers should treat the result as
    # informational-only; no fields beyond strategy + this error are
    # guaranteed to be populated.
    error: str = ""


def _api_key() -> str:
    return (os.getenv("GOOGLE_PAGESPEED_API_KEY") or "").strip()


def _safe_score(category: dict[str, Any] | None) -> int | None:
    """Convert Lighthouse's 0.0-1.0 category.score to a 0-100 int.

    Lighthouse occasionally returns None for categories that didn't run
    (Performance is sometimes skipped on hostile origins); pass that
    through as None.
    """
    if not isinstance(category, dict):
        return None
    raw = category.get("score")
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def _audit_metric(audits: dict[str, Any], audit_id: str,
                  *, field: str = "numericValue") -> float | None:
    """Pull a single numeric audit value (e.g. largest-contentful-paint's
    numericValue is the LCP in milliseconds)."""
    a = audits.get(audit_id)
    if not isinstance(a, dict):
        return None
    raw = a.get(field)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def audit_pagespeed(
    url: str,
    *,
    strategy: str = _DEFAULT_STRATEGY,
    api_key: str | None = None,
) -> PageSpeedResult:
    """Run a PageSpeed Insights audit. Never raises.

    ``api_key`` overrides the env-var lookup (useful for tests). When unset
    + no env var, returns ``PageSpeedResult(error="api_key_unset")``.
    """
    key = (api_key if api_key is not None else _api_key()).strip()
    if not key:
        return PageSpeedResult(strategy=strategy, error="api_key_unset")

    params = {
        "url": url,
        "key": key,
        "strategy": strategy,
        # Request all 4 categories; mobile strategy is the default rank-
        # affecting view for local services.
        "category": ["performance", "accessibility", "seo", "best-practices"],
    }
    headers = {"User-Agent": _USER_AGENT}
    try:
        client = _get_shared_client()
        r = client.get(_PSI_ENDPOINT, params=params, headers=headers)
    except httpx.HTTPError as exc:
        _LOG.warning("pagespeed transport failure url=%s err=%s", url, exc)
        return PageSpeedResult(strategy=strategy, error=f"transport_error: {exc}")

    if r.status_code != 200:
        # Surface the upstream error to the audit row so operator sees it.
        body_preview = (r.text or "")[:200]
        return PageSpeedResult(
            strategy=strategy,
            error=f"http_{r.status_code}: {body_preview}",
        )

    try:
        body = r.json()
    except ValueError as exc:
        return PageSpeedResult(strategy=strategy, error=f"invalid_json: {exc}")

    lighthouse = (body or {}).get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}

    lcp_raw = _audit_metric(audits, "largest-contentful-paint")
    cls_raw = _audit_metric(audits, "cumulative-layout-shift")
    tbt_raw = _audit_metric(audits, "total-blocking-time")
    fcp_raw = _audit_metric(audits, "first-contentful-paint")
    si_raw = _audit_metric(audits, "speed-index")

    return PageSpeedResult(
        strategy=strategy,
        performance_score=_safe_score(categories.get("performance")),
        accessibility_score=_safe_score(categories.get("accessibility")),
        seo_score=_safe_score(categories.get("seo")),
        best_practices_score=_safe_score(categories.get("best-practices")),
        lcp_ms=int(round(lcp_raw)) if lcp_raw is not None else None,
        cls=round(cls_raw, 3) if cls_raw is not None else None,
        tbt_ms=int(round(tbt_raw)) if tbt_raw is not None else None,
        fcp_ms=int(round(fcp_raw)) if fcp_raw is not None else None,
        speed_index_ms=int(round(si_raw)) if si_raw is not None else None,
        error="",
    )


__all__ = ["PageSpeedResult", "audit_pagespeed"]
