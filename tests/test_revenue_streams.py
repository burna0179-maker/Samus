"""Tests for backend.finance.revenue_streams — income partition (active)."""
from __future__ import annotations

from types import SimpleNamespace

from backend.finance import revenue_streams as rs


def _settings(**over):
    base = dict(
        stripe_api_key="sk_default", stripe_api_key_products="", stripe_api_key_tiktok="",
        stripe_webhook_secret="whsec_default", stripe_webhook_secret_products="",
        stripe_webhook_secret_tiktok="", revenue_entity_default="HustleForge LLC",
        revenue_entity_products="", revenue_entity_tiktok="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_default_stream_uses_default_key():
    s = _settings()
    assert rs.resolve_stripe_key(rs.SERVICES, s) == "sk_default"


def test_unarmed_stream_pools_under_default():
    s = _settings()  # products key empty
    assert rs.resolve_stripe_key(rs.PRODUCTS, s) == "sk_default"
    assert rs.has_own_account(rs.PRODUCTS, s) is False


def test_armed_stream_uses_own_key():
    s = _settings(stripe_api_key_products="sk_products")
    assert rs.resolve_stripe_key(rs.PRODUCTS, s) == "sk_products"
    assert rs.has_own_account(rs.PRODUCTS, s) is True


def test_entity_defaults_to_llc_and_overrides():
    s = _settings()
    assert rs.entity_for(rs.PRODUCTS, s) == "HustleForge LLC"
    s2 = _settings(revenue_entity_products="Forge Goods LLC")
    assert rs.entity_for(rs.PRODUCTS, s2) == "Forge Goods LLC"


def test_attribute_stamps_stream_and_entity():
    s = _settings(revenue_entity_tiktok="Forge Media LLC")
    rec = rs.attribute({"amount": 100}, rs.TIKTOK_SHOP, s)
    assert rec["revenue_stream"] == "tiktok_shop"
    assert rec["entity"] == "Forge Media LLC"


def test_stripe_client_for_returns_none_without_key():
    s = _settings(stripe_api_key="")
    assert rs.stripe_client_for(rs.SERVICES, s) is None


def test_stripe_client_for_constructs_with_resolved_key():
    s = _settings(stripe_api_key_tiktok="sk_tiktok")
    client = rs.stripe_client_for(rs.TIKTOK_SHOP, s)
    assert client is not None
    assert client._api_key == "sk_tiktok"  # noqa: SLF001 — test asserts the bound key


def test_stream_summary_reports_partition_state():
    s = _settings(stripe_api_key_products="sk_products")
    summary = {row["stream_id"]: row for row in rs.stream_summary(s)}
    assert summary["products"]["own_account"] is True
    assert summary["tiktok_shop"]["own_account"] is False
    assert summary["services"]["transacts"] is True
