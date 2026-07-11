"""Digital-product fulfillment package.

Owns the 7 LIVE SKUs on hustleforge.tech (3 playbooks + 3 packs + 1 add-on
family) and the fulfillment chain that turns a paid Stripe session into a
delivered artifact + email.

Public surface:
    - ProductConfig / AddOnConfig — registry dataclasses
    - get_product / get_addon / get_any — lookup by sku_id
    - PRODUCTS / ADDONS — the registered lists
    - fulfill_digital_product — orchestrator (mirrors backend.fulfill shape)
    - FulfillmentResult — re-exported from backend.fulfill for typing convenience
"""

from __future__ import annotations

from .registry import (
    ADDONS,
    PRODUCTS,
    AddOnConfig,
    ProductConfig,
    UnknownSKUError,
    get_addon,
    get_any,
    get_product,
    list_all,
    products_root,
)
from .fulfill_digital import (
    DigitalFulfillStep,
    DigitalFulfillmentResult,
    fulfill_digital_product,
)

__all__ = [
    "ADDONS",
    "PRODUCTS",
    "AddOnConfig",
    "DigitalFulfillStep",
    "DigitalFulfillmentResult",
    "ProductConfig",
    "UnknownSKUError",
    "fulfill_digital_product",
    "get_addon",
    "get_any",
    "get_product",
    "list_all",
    "products_root",
]
