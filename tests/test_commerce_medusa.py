"""Tests for backend.commerce — Medusa client + catalog sync (dormant + fail-closed).

The _request wrapper is monkeypatched; no network.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.commerce.catalog_sync import publish_product, reconcile_orders
from backend.commerce.medusa_client import MedusaClient


def _settings(**over):
    base = dict(commerce_medusa_enabled=True, medusa_base_url="https://store.example.com",
                medusa_admin_token="tok", revenue_entity_default="HustleForge LLC",
                revenue_entity_products="")
    base.update(over)
    return SimpleNamespace(**base)


def test_unconfigured_client_makes_no_call():
    client = MedusaClient(base_url="", admin_token="")
    assert client.configured is False
    assert client.list_products() == []
    assert client.list_orders() == []
    assert client.create_product(title="X")["ok"] is False


def test_list_products_parses(monkeypatch):
    client = MedusaClient(base_url="https://store.example.com", admin_token="tok")

    def fake_request(method, path, *, json=None, params=None):
        assert path == "/products"
        return {"products": [{"id": "prod_1", "title": "Lamp",
                              "variants": [{"prices": [{"amount": 2999, "currency_code": "usd"}]}]}]}

    monkeypatch.setattr(client, "_request", fake_request)
    products = client.list_products()
    assert len(products) == 1
    assert products[0].title == "Lamp"
    assert products[0].price_usd_cents == 2999


def test_publish_product_disabled():
    res = publish_product(title="X", settings=_settings(commerce_medusa_enabled=False))
    assert res["status"] == "disabled"


def test_publish_product_creates(monkeypatch):
    client = MedusaClient(base_url="https://store.example.com", admin_token="tok")
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: {"product": {"id": "prod_9", "title": "Mini Lamp"}})
    res = publish_product(title="Mini Lamp", price_usd_cents=2999, settings=_settings(), client=client)
    assert res["ok"] is True
    assert res["revenue_stream"] == "products"
    assert res["product"]["title"] == "Mini Lamp"


def test_reconcile_orders_attributes_products_stream(monkeypatch):
    client = MedusaClient(base_url="https://store.example.com", admin_token="tok")
    monkeypatch.setattr(client, "_request", lambda *a, **k: {"orders": [
        {"id": "order_1", "display_id": "1", "email": "b@x.com", "total": 4999, "currency_code": "usd"},
        {"id": "order_2", "display_id": "2", "email": "c@x.com", "total": 2999, "currency_code": "usd"},
    ]})
    report = reconcile_orders(settings=_settings(revenue_entity_products="Forge Goods LLC"), client=client)
    assert report["status"] == "ok"
    assert report["order_count"] == 2
    assert report["gross_usd_cents"] == 7998
    assert report["entity"] == "Forge Goods LLC"
    assert all(o["revenue_stream"] == "products" for o in report["orders"])


def test_reconcile_orders_dormant_when_disabled():
    report = reconcile_orders(settings=_settings(commerce_medusa_enabled=False))
    assert report["status"] == "disabled"
    assert report["order_count"] == 0
