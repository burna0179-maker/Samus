"""Tests for the canonical SKU registry.

Asserts the CATALOG has all expected SKUs, the lookup helpers work, and the
data invariants hold (no duplicate ids, Stripe ids well-formed, retainers
flagged recurring).
"""

from __future__ import annotations

import pytest

from backend.catalog import (
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


EXPECTED_SKUS: set[str] = {
    # Services (one-time)
    "service_website_build",
    "service_workflow_rescue",
    "service_workflow_buildout",
    "service_seo_implementation",
    "service_ai_ops_partner_build",
    "seo_audit",
    # Retainers (monthly)
    "retainer_ai_ops_partner_entry",
    "retainer_ai_ops_partner_premium",
    "retainer_seo_optimization",
    "retainer_ai_receptionist",
    # Digital playbooks
    "playbook_lead_qual",
    "playbook_client_onboarding",
    "playbook_sales_followup",
    # Digital packs
    "pack_creator_quickstart",
    "pack_content_funnel",
    "pack_authority_accelerator",
    # Add-ons
    "addon_stripe_hardening",
    "addon_email_deliverability",
    "addon_404_audit",
    "addon_dns_health",
    "addon_automation_health_check",
    "addon_crm_hygiene_sweep",
    "addon_dashboard_setup",
}


# --- structural integrity ---------------------------------------------------


def test_catalog_has_all_expected_skus():
    actual = set(all_sku_ids())
    missing = EXPECTED_SKUS - actual
    extra = actual - EXPECTED_SKUS
    assert not missing, f"Missing SKUs in CATALOG: {sorted(missing)}"
    assert not extra, f"Unexpected SKUs in CATALOG: {sorted(extra)}"


def test_sku_ids_are_unique():
    ids = all_sku_ids()
    assert len(ids) == len(set(ids)), "Duplicate sku_id in CATALOG"


def test_stripe_product_ids_are_unique_per_entry_or_intentionally_shared():
    # Each Stripe product id should map to exactly one entry (no two SKUs
    # pointing at the same product), with the exception of intentional
    # reuse — currently none. If reuse is added later, update this test.
    product_ids = [e.stripe_product_id for e in CATALOG if e.stripe_product_id]
    assert len(product_ids) == len(set(product_ids)), \
        f"Duplicate stripe_product_id in CATALOG: {product_ids}"


def test_stripe_price_ids_are_unique():
    price_ids = [e.stripe_price_id for e in CATALOG if e.stripe_price_id]
    assert len(price_ids) == len(set(price_ids)), \
        f"Duplicate stripe_price_id in CATALOG: {price_ids}"


def test_every_entry_has_description():
    for e in CATALOG:
        assert e.description and e.description.strip(), \
            f"{e.sku_id} has empty description"
        assert len(e.description) > 20, \
            f"{e.sku_id} description is too short to be real: {e.description!r}"


_QUOTE_BASED_SKUS = {"service_ai_ops_partner_build"}
# SKUs added to the registry ahead of their Stripe objects — IDs are None
# until the operator runs Phase 0 (create product/price/meter) and pastes
# the live ids in. Tracked separately from quote-based so the distinction
# (deliberately-manual vs not-yet-created) stays explicit.
_STRIPE_PENDING_SKUS = {"retainer_ai_receptionist", "service_website_build"}
_NO_LIVE_STRIPE_SKUS = _QUOTE_BASED_SKUS | _STRIPE_PENDING_SKUS


def test_every_non_quote_based_entry_has_live_stripe_product_id():
    """Quote-based + Stripe-pending SKUs intentionally lack live Stripe IDs."""
    for e in CATALOG:
        if e.sku_id in _NO_LIVE_STRIPE_SKUS:
            assert e.stripe_product_id is None, \
                f"{e.sku_id} has no live Stripe product yet; must be None"
            continue
        assert e.stripe_product_id, f"{e.sku_id} missing stripe_product_id"
        assert e.stripe_product_id.startswith("prod_"), \
            f"{e.sku_id} stripe_product_id is malformed: {e.stripe_product_id}"


def test_every_non_quote_based_entry_has_live_stripe_price_id():
    for e in CATALOG:
        if e.sku_id in _NO_LIVE_STRIPE_SKUS:
            assert e.stripe_price_id is None, \
                f"{e.sku_id} has no live Stripe price yet; must be None"
            continue
        assert e.stripe_price_id, f"{e.sku_id} missing stripe_price_id"
        assert e.stripe_price_id.startswith("price_"), \
            f"{e.sku_id} stripe_price_id is malformed: {e.stripe_price_id}"


def test_retainers_are_recurring():
    for e in by_category(SkuCategory.RETAINER):
        assert e.recurring is True, f"{e.sku_id} is RETAINER but not recurring"
        assert e.fulfillment_module == FulfillmentModule.RETAINER, \
            f"{e.sku_id} RETAINER must use RETAINER fulfillment module"


def test_addons_route_via_services_or_products():
    # Add-ons split between SERVICES (operator-delivered briefs like
    # SPF/DKIM setup) and PRODUCTS (content-driven briefs like CRM
    # hygiene sweep). Either is valid; only RETAINER/LEGACY are not.
    valid = {FulfillmentModule.SERVICES, FulfillmentModule.PRODUCTS}
    for e in by_category(SkuCategory.ADDON):
        assert e.fulfillment_module in valid, \
            f"{e.sku_id} ADDON routes via unexpected module {e.fulfillment_module}"
        assert e.recurring is False, f"{e.sku_id} ADDON should be one-time"


def test_digital_use_products_fulfillment():
    for e in by_category(SkuCategory.DIGITAL):
        assert e.fulfillment_module == FulfillmentModule.PRODUCTS, \
            f"{e.sku_id} DIGITAL must route via PRODUCTS fulfillment"


def test_price_cents_positive():
    for e in CATALOG:
        assert e.price_usd_cents > 0, f"{e.sku_id} has non-positive price"


# --- lookup helpers ---------------------------------------------------------


def test_sku_lookup_returns_entry():
    entry = sku("service_workflow_rescue")
    assert isinstance(entry, CatalogEntry)
    assert entry.sku_id == "service_workflow_rescue"
    assert entry.display_name == "48-Hour Workflow Rescue"


def test_sku_lookup_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        sku("does_not_exist")


def test_lookup_by_stripe_product_roundtrips():
    for e in CATALOG:
        if e.stripe_product_id:
            assert lookup_by_stripe_product(e.stripe_product_id) is e


def test_lookup_by_stripe_product_unknown_returns_none():
    assert lookup_by_stripe_product("prod_nonexistent") is None


def test_lookup_by_stripe_price_roundtrips():
    for e in CATALOG:
        if e.stripe_price_id:
            assert lookup_by_stripe_price(e.stripe_price_id) is e


def test_lookup_by_stripe_price_unknown_returns_none():
    assert lookup_by_stripe_price("price_nonexistent") is None


def test_by_category_partitions_catalog():
    total = sum(len(by_category(c)) for c in SkuCategory)
    assert total == len(CATALOG), \
        "by_category() returned overlapping or missing entries"


def test_by_category_counts():
    # Lock in the expected category breakdown so unintentional miscategorization
    # is caught.
    assert len(by_category(SkuCategory.SERVICE)) == 6
    assert len(by_category(SkuCategory.RETAINER)) == 4
    assert len(by_category(SkuCategory.DIGITAL)) == 6
    assert len(by_category(SkuCategory.ADDON)) == 7


# --- new-product spot checks ------------------------------------------------


def test_ai_ops_partner_build_entry():
    """One-time build, quote-based — no static Stripe IDs."""
    entry = sku("service_ai_ops_partner_build")
    assert entry.category == SkuCategory.SERVICE
    assert entry.fulfillment_module == FulfillmentModule.SERVICES
    assert entry.recurring is False
    assert entry.price_usd_cents == 200000   # $2,000 floor
    assert entry.stripe_product_id is None
    assert entry.stripe_price_id is None
    assert entry.payment_link_url is None


def test_ai_ops_partner_entry_retainer():
    """$2k/mo entry tier matching the public page; reuses original AI Ops Stripe product."""
    e = sku("retainer_ai_ops_partner_entry")
    assert e.category == SkuCategory.RETAINER
    assert e.fulfillment_module == FulfillmentModule.RETAINER
    assert e.recurring is True
    assert e.price_usd_cents == 200000
    assert e.stripe_product_id == "prod_U913mXXZXG9REI"
    assert e.stripe_price_id == "price_1TAj1pBdo4u4gpS7GZGee1zn"


def test_ai_ops_partner_premium_retainer():
    """$5k/mo premium tier, follows the Build engagement."""
    p = sku("retainer_ai_ops_partner_premium")
    assert p.category == SkuCategory.RETAINER
    assert p.fulfillment_module == FulfillmentModule.RETAINER
    assert p.recurring is True
    assert p.price_usd_cents == 500000
    assert p.stripe_product_id == "prod_UWt4i2QB7BZlFx"
    assert p.stripe_price_id == "price_1TXpJEBdo4u4gpS7p3kkVPlt"


def test_addon_stripe_hardening_entry():
    entry = sku("addon_stripe_hardening")
    assert entry.category == SkuCategory.ADDON
    assert entry.price_usd_cents == 4999
    assert entry.stripe_product_id == "prod_UWt5OLlYT4RJtM"


def test_all_addons_have_payment_links():
    for e in by_category(SkuCategory.ADDON):
        assert e.payment_link_url and e.payment_link_url.startswith("https://buy.stripe.com/"), \
            f"{e.sku_id} add-on missing payment_link_url"
