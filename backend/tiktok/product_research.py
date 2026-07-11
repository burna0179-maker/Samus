"""Trending-product research via a pluggable provider.

COMPLIANCE: TikTok's official Research API prohibits commercial use — using it to
drive selling or a paid tool risks permanent revocation. So this module never
calls it for sourcing. Instead the provider is operator-selected:

* ``none`` (default) — returns ``[]`` and spends nothing (dormant).
* ``netrows`` / ``echotik`` — commercial-cleared third-party TikTok Shop data
  APIs (operator supplies ``tiktok_research_api_key``).
* ``custom`` — any HTTP endpoint the operator points at via
  ``tiktok_research_base_url`` that returns ``{"products": [...]}``.

The thin ``_http_get`` wrapper is the test monkeypatch point. Never raises.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.tiktok.models import TrendingProduct

_LOG = logging.getLogger("samus.tiktok.research")

# Known commercial providers -> their query base URL. `custom` reads the base
# URL from settings. These are deliberately data-only (no official Research API).
_PROVIDER_BASE = {
    "netrows": "https://api.netrows.com/tiktok/shop/products",
    "echotik": "https://api.echotik.live/v1/products",
}
_TIMEOUT = 15.0


def _http_get(url: str, *, headers: dict[str, str], timeout: float = _TIMEOUT) -> httpx.Response:
    """Thin wrapper — the test monkeypatch point."""
    with httpx.Client(timeout=timeout) as client:
        return client.get(url, headers=headers)


def find_trending(query: str, *, settings: Any, limit: int = 20) -> list[TrendingProduct]:
    """Return trending products for ``query`` via the configured provider.

    Dormant + fail-closed: provider ``none`` (default) or a missing key/base URL
    returns ``[]`` without a network call. Never raises.
    """
    provider = (getattr(settings, "tiktok_research_provider", "none") or "none").strip().lower()
    if provider == "none":
        return []

    base = _PROVIDER_BASE.get(provider) or (getattr(settings, "tiktok_research_base_url", "") or "").strip()
    if not base:
        _LOG.info("tiktok research: provider %s has no base url", provider)
        return []
    api_key = (getattr(settings, "tiktok_research_api_key", "") or "").strip()
    if not api_key:
        _LOG.info("tiktok research: provider %s missing api key", provider)
        return []

    url = f"{base}?{urlencode({'q': query, 'limit': max(1, min(100, int(limit)))})}"
    try:
        resp = _http_get(url, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            _LOG.warning("tiktok research http %s", resp.status_code)
            return []
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _LOG.warning("tiktok research failed: %s", exc)
        return []

    rows = payload.get("products") or payload.get("data") or []
    out: list[TrendingProduct] = []
    for row in rows:
        if isinstance(row, dict):
            tp = TrendingProduct.from_provider(row)
            if tp.title:
                out.append(tp)
    return out[:limit]


def rank_by_opportunity(products: list[TrendingProduct]) -> list[TrendingProduct]:
    """Deterministic sourcing rank: high sales + rating, sane price. Pure/$0."""
    def score(p: TrendingProduct) -> float:
        price_fit = 1.0 if 8.0 <= p.price_usd <= 60.0 else 0.5
        return (p.sales * 1.0) + (p.rating * 200.0) * price_fit
    return sorted(products, key=score, reverse=True)


__all__ = ["find_trending", "rank_by_opportunity"]
