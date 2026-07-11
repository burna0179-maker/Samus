"""TikTok Shop Seller API — DORMANT until an approved seller app is armed.

Selling on TikTok Shop requires an operator-approved TikTok Shop **seller account
+ app review** (Commerce category). Until ``tiktok_shop_enabled`` is on AND the
app credentials are seeded, every method here fails closed (returns a structured
result, makes no network call, never raises). Built to the real Seller API shape
so arming it is a credential change, not a rewrite.

Orders pulled back attribute to the ``tiktok_shop`` revenue stream
(:mod:`backend.finance.revenue_streams`) so the income separates by source/entity.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.finance import revenue_streams

_LOG = logging.getLogger("samus.tiktok.shop_seller")

_BASE = "https://open-api.tiktokglobalshop.com"
_TIMEOUT = 20.0


def _armed(settings: Any) -> tuple[bool, str]:
    if not bool(getattr(settings, "tiktok_shop_enabled", False)):
        return False, "disabled"
    token = (getattr(settings, "tiktok_shop_access_token", "") or "").strip()
    if not token:
        return False, "no_access_token"
    return True, token


def _http(
    method: str,
    url: str,
    *,
    token: str,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Thin wrapper — the test monkeypatch point."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        return client.request(
            method,
            url,
            json=json,
            params=params,
            headers={"x-tts-access-token": token, "Content-Type": "application/json"},
        )


def list_shop_products(*, settings: Any, limit: int = 50) -> dict[str, Any]:
    armed, token = _armed(settings)
    if not armed:
        return {"status": token, "products": []}
    try:
        resp = _http(
            "GET",
            f"{_BASE}/product/202309/products/search",
            token=token,
            params={"page_size": max(1, min(100, limit))},
        )
        if resp.status_code != 200:
            return {"status": f"http_{resp.status_code}", "products": []}
        data = resp.json()
        return {"status": "ok", "products": (data.get("data") or {}).get("products") or []}
    except (httpx.HTTPError, ValueError) as exc:
        _LOG.warning("tiktok shop list_products failed: %s", exc)
        return {"status": "error", "error": str(exc), "products": []}


def create_listing(
    *, title: str, description: str, price_usd: float, settings: Any
) -> dict[str, Any]:
    armed, token = _armed(settings)
    if not armed:
        return {"ok": False, "status": token}
    try:
        resp = _http(
            "POST",
            f"{_BASE}/product/202309/products",
            token=token,
            json={
                "title": title,
                "description": description,
                "skus": [{"price": {"amount": str(price_usd), "currency": "USD"}}],
            },
        )
        if resp.status_code != 200:
            return {"ok": False, "status": f"http_{resp.status_code}", "error": resp.text[:200]}
        return {"ok": True, "status": "created", "data": resp.json().get("data") or {}}
    except (httpx.HTTPError, ValueError) as exc:
        _LOG.warning("tiktok shop create_listing failed: %s", exc)
        return {"ok": False, "status": "error", "error": str(exc)}


def fetch_orders(*, settings: Any, limit: int = 50) -> dict[str, Any]:
    """Pull TikTok Shop orders, attributed to the tiktok_shop revenue stream."""
    armed, token = _armed(settings)
    if not armed:
        return {"status": token, "order_count": 0, "orders": []}
    try:
        resp = _http(
            "GET",
            f"{_BASE}/order/202309/orders/search",
            token=token,
            params={"page_size": max(1, min(100, limit))},
        )
        if resp.status_code != 200:
            return {"status": f"http_{resp.status_code}", "order_count": 0, "orders": []}
        rows = (resp.json().get("data") or {}).get("orders") or []
    except (httpx.HTTPError, ValueError) as exc:
        _LOG.warning("tiktok shop fetch_orders failed: %s", exc)
        return {"status": "error", "error": str(exc), "order_count": 0, "orders": []}

    out = []
    for row in rows:
        rec = dict(row) if isinstance(row, dict) else {"raw": row}
        revenue_streams.attribute(rec, revenue_streams.TIKTOK_SHOP, settings)
        out.append(rec)
    return {
        "status": "ok",
        "order_count": len(out),
        "entity": revenue_streams.entity_for(revenue_streams.TIKTOK_SHOP, settings),
        "orders": out,
    }


__all__ = ["list_shop_products", "create_listing", "fetch_orders"]
