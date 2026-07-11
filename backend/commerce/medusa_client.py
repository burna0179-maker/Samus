"""Thin httpx client for a Medusa instance's Admin REST API.

DORMANT + keyless-safe, mirroring ``finance/stripe_client.py`` +
``website/deploy_cloudflare.py``: with no ``medusa_base_url`` / ``medusa_admin_token``
the client is "unconfigured" and read methods return empty lists / writes return
a structured ``{"ok": False, "error": "medusa_unconfigured"}`` — it never raises
and never makes a network call. The single ``_request`` wrapper is the test
monkeypatch point.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.commerce.models import MedusaOrder, MedusaProduct

_LOG = logging.getLogger("samus.commerce.medusa")

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=15.0, pool=5.0)


class MedusaClient:
    """Minimal Medusa Admin API client. One per call; key passed at construction."""

    def __init__(
        self, base_url: str, admin_token: str, *, timeout: httpx.Timeout = _TIMEOUT
    ) -> None:
        self._base = (base_url or "").strip().rstrip("/")
        self._token = (admin_token or "").strip()
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Any) -> "MedusaClient":
        return cls(
            getattr(settings, "medusa_base_url", "") or "",
            getattr(settings, "medusa_admin_token", "") or "",
        )

    @property
    def configured(self) -> bool:
        return bool(self._base and self._token)

    # --- low-level (the monkeypatch point) --------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make one Admin API call. Returns parsed JSON. Raises httpx.HTTPError /
        ValueError on failure (callers wrap fail-closed)."""
        url = f"{self._base}/admin{path}"
        headers = {"x-medusa-access-token": self._token, "Content-Type": "application/json"}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.request(method, url, json=json, params=params, headers=headers)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"medusa {resp.status_code}: {resp.text[:200]}", request=resp.request, response=resp
            )
        return resp.json() if resp.content else {}

    # --- products ----------------------------------------------------------

    def list_products(self, *, limit: int = 50) -> list[MedusaProduct]:
        if not self.configured:
            return []
        try:
            data = self._request("GET", "/products", params={"limit": max(1, min(100, limit))})
        except (httpx.HTTPError, ValueError) as exc:
            _LOG.warning("medusa list_products failed: %s", exc)
            return []
        return [MedusaProduct.from_api(r) for r in (data.get("products") or [])]

    def create_product(
        self,
        *,
        title: str,
        description: str = "",
        price_usd_cents: int = 0,
        thumbnail: str = "",
        status: str = "draft",
    ) -> dict[str, Any]:
        if not self.configured:
            return {"ok": False, "error": "medusa_unconfigured"}
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "status": status,
        }
        if thumbnail:
            body["thumbnail"] = thumbnail
        if price_usd_cents > 0:
            body["variants"] = [
                {
                    "title": "Default",
                    "prices": [{"amount": int(price_usd_cents), "currency_code": "usd"}],
                }
            ]
        try:
            data = self._request("POST", "/products", json=body)
        except (httpx.HTTPError, ValueError) as exc:
            _LOG.warning("medusa create_product failed: %s", exc)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        product = data.get("product") or {}
        return {"ok": True, "product": MedusaProduct.from_api(product).to_dict()}

    # --- orders ------------------------------------------------------------

    def list_orders(self, *, limit: int = 50) -> list[MedusaOrder]:
        if not self.configured:
            return []
        try:
            data = self._request("GET", "/orders", params={"limit": max(1, min(100, limit))})
        except (httpx.HTTPError, ValueError) as exc:
            _LOG.warning("medusa list_orders failed: %s", exc)
            return []
        return [MedusaOrder.from_api(r) for r in (data.get("orders") or [])]

    def retrieve_order(self, order_id: str) -> MedusaOrder | None:
        if not self.configured or not order_id:
            return None
        try:
            data = self._request("GET", f"/orders/{order_id}")
        except (httpx.HTTPError, ValueError) as exc:
            _LOG.warning("medusa retrieve_order failed: %s", exc)
            return None
        row = data.get("order")
        return MedusaOrder.from_api(row) if row else None


__all__ = ["MedusaClient"]
