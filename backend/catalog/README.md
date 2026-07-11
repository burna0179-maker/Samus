# backend/catalog — canonical SKU registry

This package is the **single source of truth** for every product Samus sells.
It maps:

    site SKU id  <->  Stripe product id  <->  Stripe price id  <->  fulfillment module  <->  price

If you change a SKU anywhere else without updating this registry, the gateway
will reject the webhook because `lookup_by_stripe_product` will return `None`.

## Truth-source contract

1. **Every site SKU has exactly one `CatalogEntry`** in `registry.py:CATALOG`.
2. **Stripe IDs in this file are LIVE.** The account is
   `acct_1SQRZABdo4u4gpS7` ("HustleForge"). Don't paste test-mode IDs here.
3. **The registry mirrors Stripe.** When the live Stripe price differs from
   the spec (e.g. canonical table says $750 but Stripe is set at $200), the
   registry holds the **live** value and the discrepancy is logged in
   `stripe_sync_report.md` for the operator to reconcile.
4. **No placeholders.** Every entry has a real description and either a real
   Stripe id triplet or an explicit `None` (only when no Stripe artifact
   exists yet).

## How to add a new SKU

1. Create the Stripe product, price, and payment link in the live account
   (use the Stripe MCP or dashboard).
2. Add a `CatalogEntry(...)` to `CATALOG` in `registry.py` with all fields
   populated.
3. Pick the right `FulfillmentModule`:
   - `PRODUCTS` — digital downloads (`backend.products.fulfill_digital`)
   - `SERVICES` — one-time human services (`backend.services.fulfill_service`)
   - `RETAINER` — recurring monthly engagement (`backend.retainer.enroll`)
   - `LEGACY_SEO_AUDIT` — original SEO audit only (`backend.fulfill`)
4. Append a line to `stripe_sync_report.md` recording the create.
5. Add the new `sku_id` to `tests/test_catalog_registry.py:EXPECTED_SKUS`.

## How to change an existing SKU

- **Price change**: edit `price_usd_cents` in the entry **and** create a new
  Stripe price (Stripe prices are immutable). Update `stripe_price_id` and
  `payment_link_url` to point at the new price. Old price stays active in
  Stripe for existing subscriptions.
- **Display name / description**: edit the entry and update the Stripe product
  via the dashboard or MCP. The registry name is what shows on the site.
- **Fulfillment module move**: edit `fulfillment_module`. The downstream
  module must accept the SKU before you flip this.

## Lookup helpers

```python
from backend.catalog import sku, lookup_by_stripe_product, by_category, SkuCategory

entry = sku("service_workflow_rescue")
entry = lookup_by_stripe_product("prod_U913rQk3aII2T4")
retainers = by_category(SkuCategory.RETAINER)
```

`sku()` raises `KeyError` for unknown ids. `lookup_by_stripe_*` returns `None`.
