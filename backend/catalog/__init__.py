"""Canonical SKU registry for the Samus storefront.

This module is the single source of truth that maps every site SKU to its
Stripe product ID, Stripe price ID, payment link, and fulfillment module.

Public surface:

    from backend.catalog import (
        CATALOG,
        CatalogEntry,
        SkuCategory,
        FulfillmentModule,
        sku,
        lookup_by_stripe_product,
        lookup_by_stripe_price,
        by_category,
        all_sku_ids,
    )

See ``README.md`` for the contract on adding/modifying entries.
See ``stripe_sync_report.md`` for the 2026-05-16 audit record.
"""

from .registry import (
    CATALOG,
    CatalogEntry,
    FulfillmentModule,
    SkuCategory,
    all_sku_ids,
    by_category,
    lookup_by_stripe_price,
    lookup_by_stripe_product,
    sku,
)

__all__ = [
    "CATALOG",
    "CatalogEntry",
    "FulfillmentModule",
    "SkuCategory",
    "all_sku_ids",
    "by_category",
    "lookup_by_stripe_price",
    "lookup_by_stripe_product",
    "sku",
]
