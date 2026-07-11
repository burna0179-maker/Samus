"""Catalog sync + order reconciliation between Samus and Medusa.

* :func:`publish_product` pushes a sourced product (e.g. a TikTok-trending item)
  into Medusa as a draft product.
* :func:`reconcile_orders` pulls Medusa orders and attributes each to the
  ``products`` revenue stream (:mod:`backend.finance.revenue_streams`) so the
  income books cleanly separate by source/entity.

Both are dormant + fail-closed: with Medusa unconfigured they return empty/typed
results and never raise.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.commerce.medusa_client import MedusaClient
from backend.finance import revenue_streams

_LOG = logging.getLogger("samus.commerce.catalog_sync")


def publish_product(
    *,
    title: str,
    description: str = "",
    price_usd_cents: int = 0,
    thumbnail: str = "",
    settings: Any,
    client: MedusaClient | None = None,
) -> dict[str, Any]:
    """Create a draft product in Medusa from a sourced item. Returns a structured
    result. Dormant when ``commerce_medusa_enabled`` is off."""
    if not bool(getattr(settings, "commerce_medusa_enabled", False)):
        return {"ok": False, "status": "disabled"}
    client = client or MedusaClient.from_settings(settings)
    if not client.configured:
        return {"ok": False, "status": "unconfigured"}
    result = client.create_product(
        title=title, description=description, price_usd_cents=price_usd_cents,
        thumbnail=thumbnail, status="draft",
    )
    result["revenue_stream"] = revenue_streams.PRODUCTS
    return result


def reconcile_orders(
    *, settings: Any, limit: int = 50, client: MedusaClient | None = None,
) -> dict[str, Any]:
    """Pull Medusa orders and attribute them to the `products` stream.

    Returns ``{status, order_count, gross_usd_cents, entity, orders:[...]}``. The
    orders carry ``revenue_stream`` + ``entity`` stamps for the books. Dormant +
    fail-closed.
    """
    if not bool(getattr(settings, "commerce_medusa_enabled", False)):
        return {"status": "disabled", "order_count": 0, "orders": []}
    client = client or MedusaClient.from_settings(settings)
    if not client.configured:
        return {"status": "unconfigured", "order_count": 0, "orders": []}

    orders = client.list_orders(limit=limit)
    rows: list[dict[str, Any]] = []
    gross = 0
    for o in orders:
        rec = o.to_dict()
        revenue_streams.attribute(rec, revenue_streams.PRODUCTS, settings)
        gross += o.total_usd_cents
        rows.append(rec)

    return {
        "status": "ok",
        "order_count": len(rows),
        "gross_usd_cents": gross,
        "entity": revenue_streams.entity_for(revenue_streams.PRODUCTS, settings),
        "orders": rows,
    }


__all__ = ["publish_product", "reconcile_orders"]
