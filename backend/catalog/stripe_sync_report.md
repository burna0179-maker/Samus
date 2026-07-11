# Stripe sync report — 2026-05-16

Audit + reconcile pass run by Stream 4 of the Samus deliverable-fulfillment
rebuild. Live Stripe MCP mutations were authorized by operator (Alex).

## Account verified

- **Account name**: HustleForge
- **Account ID**: `acct_1SQRZABdo4u4gpS7`
- **Livemode**: **TRUE** (confirmed via publishable key prefix `pk_live_` in
  `Samus/recovery/onboarding_form_schema.py:99` and verified through
  `mcp__claude_ai_Stripe__get_stripe_account_info`).
- All product/price/payment_link mutations below ran against the live account.

## Existing products matched to canonical SKUs (no mutation)

| Canonical SKU | Stripe product id | Stripe price id | Notes |
|---|---|---|---|
| `service_workflow_rescue` | `prod_U913rQk3aII2T4` | `price_1TAj1nBdo4u4gpS7Z4fGaYT0` | $500 one-time. Matches spec. Existing buy link `cNi14m74C1ay9Ke0ja8so0c`. |
| `service_workflow_buildout` | `prod_U913HTNX3D0lMH` | `price_1TAj1pBdo4u4gpS7JBbZ5SWJ` | $2,500 one-time. Spec calls for $2,750 — see Discrepancies. |
| `service_seo_implementation` | `prod_UJXJbkZfIZOM5a` | `price_1TKuEeBdo4u4gpS7853xVNwz` | $200 one-time. Spec calls for $750 — see Discrepancies. Existing buy link `dRm4gy0Geg5sg8Cea08so0e`. |
| `service_seo_automation` | `prod_UJX6acYD2IynBf` | `price_1TKu1pBdo4u4gpS7s14QPFkd` | $500/month recurring. Spec calls for $1,500 one-time — see Discrepancies. Existing buy link `8x214m3Sqf1ocWqaXO8so0f`. |
| `seo_audit` | `prod_UJWzOjmJrxZxQq` | `price_1TKtvJBdo4u4gpS70PJf01xN` | $149 one-time. Spec calls for $500 — see Discrepancies. Existing buy link `6oU00i3Sq1ay1dId5W8so0d`. |
| `retainer_ai_ops_partner_starter` | `prod_U913mXXZXG9REI` | `price_1TAj1pBdo4u4gpS7GZGee1zn` | $2,000/month recurring. Matches spec. Product is the existing "AI Ops Partner"; reused as the Starter tier. |
| `retainer_seo_optimization` | `prod_UJX2ZiARBH88n0` | `price_1TKu0aBdo4u4gpS7I6ZPKIy6` | $300/month recurring. Matches spec. Existing buy link `cNi5kCex42eC09E1ne8so0g`. |
| `playbook_lead_qual` | `prod_U913nsMud12tVp` | `price_1TAj1sBdo4u4gpS7FZtd5A6q` | $390 one-time. Matches spec. |
| `playbook_client_onboarding` | `prod_U913b1h7cGXVwH` | `price_1TAj1rBdo4u4gpS7zoeRhJEP` | $490 one-time. Matches spec. |
| `playbook_sales_followup` | `prod_U913ApbXvKqJ7d` | `price_1TAj1qBdo4u4gpS7PPBq3VAW` | $390 one-time. Matches spec. |
| `pack_creator_quickstart` | `prod_U914lCS5MSDPKN` | `price_1TAj1sBdo4u4gpS7JFRQtBuj` | $150 one-time. Matches spec. |
| `pack_content_funnel` | `prod_U914ZfY9YY9ncl` | `price_1TAj1tBdo4u4gpS7oL1R8dzv` | $300 one-time. Matches spec. |
| `pack_authority_accelerator` | `prod_U914wnm1xgBDqM` | `price_1TAj1uBdo4u4gpS7Ve32ttJR` | $500 one-time. Matches spec. |

13 canonical SKUs already had live Stripe products in the account — most
appear to have been created in a prior batch (the `prod_U91...` series) but
were never wired back into the site's `onboarding_form_schema.STRIPE_PRODUCTS`
dict, which only references the 5 SEO/Workflow buy buttons.

## Products created in this run

All created at 2026-05-16. Six new products, six new prices, six new payment
links.

| Canonical SKU | Stripe product id | Stripe price id | Payment link |
|---|---|---|---|
| `retainer_ai_ops_partner_growth` | `prod_UWt4sLRjTxpogG` | `price_1TXpJBBdo4u4gpS7MEL3izPB` | https://buy.stripe.com/cNi28q0Ge9H46y25Du8so0m |
| `retainer_ai_ops_partner_scale` | `prod_UWt4i2QB7BZlFx` | `price_1TXpJEBdo4u4gpS7p3kkVPlt` | https://buy.stripe.com/4gMeVcfB82eC9Ke0ja8so0n |
| `addon_stripe_hardening` | `prod_UWt5OLlYT4RJtM` | `price_1TXpJIBdo4u4gpS7hWXSn0EL` | https://buy.stripe.com/dRm9AS4Wu5qO4pU4zq8so0o |
| `addon_email_deliverability` | `prod_UWt5lw09y8X5Px` | `price_1TXpJLBdo4u4gpS7H8PJEYVF` | https://buy.stripe.com/bJe3cu74Cf1of4y7LC8so0p |
| `addon_404_audit` | `prod_UWt5uHnQTEr3RK` | `price_1TXpJUBdo4u4gpS7DPd7RKeg` | https://buy.stripe.com/5kQ3cu74C4mK9Ke4zq8so0q |
| `addon_dns_health` | `prod_UWt5xWZqXGMGsY` | `price_1TXpJYBdo4u4gpS7IxXDPTKw` | https://buy.stripe.com/5kQcN4agOg5s5tY3vm8so0r |

Growth + Scale prices are recurring monthly. The four add-ons are one-time.

## Products that failed to create

None. All six creates returned 200.

## Discrepancies flagged for operator action

These are price or recurrence mismatches between the canonical SKU table and
the live Stripe state. The registry mirrors the **live** Stripe value so
checkout integrity is preserved; the operator must decide whether to update
Stripe (creating a new price; Stripe prices are immutable) or update the spec.

1. **`service_workflow_buildout`** — spec says $2,750 one-time; live Stripe
   price is **$2,500** one-time (`price_1TAj1pBdo4u4gpS7JBbZ5SWJ`). Off by
   $250.
2. **`service_seo_implementation`** — spec says $750 one-time; live Stripe
   price is **$200** one-time (`price_1TKuEeBdo4u4gpS7853xVNwz`). Off by $550.
3. **`service_seo_automation`** — spec says $1,500 one-time; live Stripe
   price is **$500/month recurring** (`price_1TKu1pBdo4u4gpS7s14QPFkd`).
   Different price AND different model. Major decision needed: is this a
   one-time service ($1,500) or the recurring SEO+Automation system
   ($500/mo)?
4. **`seo_audit`** — spec says $500 one-time; live Stripe price is **$149**
   one-time (`price_1TKtvJBdo4u4gpS70PJf01xN`). Off by $351.
5. **`retainer_ai_ops_partner_starter`** — the existing "AI Ops Partner"
   product (`prod_U913mXXZXG9REI`) is reused as the Starter tier because its
   price ($2,000/month) already matches the Starter spec exactly. Consider
   renaming the Stripe product to "AI Ops Partner — Starter" for clarity (no
   risk to existing subscriptions; product name is metadata only).
6. **Test product `prod_TbCKcqydqzisie`** ("test") still exists in the live
   account with three $0.01 prices. Recommend archiving via the dashboard.
7. Many older legacy products from a prior storefront (the
   `prod_TRs*` series — UGC, Cloud Support, automation subscription tiers,
   etc.) are still **active** in Stripe but not mapped to any canonical SKU.
   They will not break anything but the dashboard product list is cluttered.
   Consider bulk-archiving the unused ones.

## Audit constraints honored

- `list_products` returned 33 active products — over the 50-product trip-wire
  was not hit, so mutations proceeded.
- Total creates this session: **6 products** (under the 15-create stop
  condition).
- Every create was preceded by a name-match check against
  `list_products`; no duplicates were introduced.

---

# Addendum — AI Digital Receptionist (PENDING operator)

The `retainer_ai_receptionist` SKU was added to the registries
(`backend/retainer/registry.py`, `backend/catalog/registry.py`) ahead of its
Stripe objects. It is the first **metered** SKU on the account — no Stripe
Meter or `usage_type=metered` price exists yet.

**Operator must create the following on `acct_1SQRZABdo4u4gpS7` (Phase 0), in
TEST mode first for verification, then in live mode**, and paste the resulting
IDs into the two registry files (the placeholders are currently `None`):

| Object | Spec | Registry field to populate |
|---|---|---|
| Product "AI Digital Receptionist" | — | `stripe_product_id` (both files) |
| Flat base price | recurring, `usage_type=licensed`, monthly, `unit_amount=9900` | `stripe_price_id` (both files) |
| Meter | `event_name=receptionist_call_minutes`, aggregation **sum** over payload key `value` | (matches the already-pinned `stripe_meter_event_name`) |
| Metered price | recurring, `usage_type=metered`, monthly, `unit_amount=35`, `recurring[meter]` = the Meter above | `stripe_overage_price_id` (retainer registry only) |
| Setup-fee price | one-time, `unit_amount=25000` | runbook only — billed as a separate one-off invoice, not cataloged |

Object graph: one product carries all three prices; the base + metered prices
sit on ONE two-item subscription per client; the $250 setup is a separate
one-off invoice (keeps the first-invoice MRR proxy clean).

The metered price is intentionally NOT added to `backend/catalog/registry.py`
— a usage-billed invoice line is not a purchase event and must not resolve to
a fulfillment module. The catalog maps only the flat base price.
