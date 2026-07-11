"""Tests for backend.tiktok — research, campaigns, dormant seller (fail-closed)."""
from __future__ import annotations

from types import SimpleNamespace

import backend.tiktok.campaigns as campaigns_mod
import backend.tiktok.product_research as research_mod
from backend.tiktok import shop_seller
from backend.tiktok.campaigns import build_caption, build_product_reel, post_to_tiktok, run_campaign
from backend.tiktok.models import CampaignResult, TrendingProduct
from backend.tiktok.product_research import find_trending, rank_by_opportunity


def _settings(**over):
    base = dict(
        tiktok_research_provider="none", tiktok_research_api_key="", tiktok_research_base_url="",
        tiktok_content_posting_enabled=False, tiktok_content_access_token="", tiktok_dry_run=True,
        tiktok_shop_enabled=False, tiktok_shop_access_token="",
        revenue_entity_default="HustleForge LLC", revenue_entity_tiktok="",
    )
    base.update(over)
    return SimpleNamespace(**base)


# --- research ---------------------------------------------------------------

def test_research_provider_none_returns_empty(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(research_mod, "_http_get", lambda *a, **k: called.__setitem__("n", 1))
    assert find_trending("gadgets", settings=_settings()) == []
    assert called["n"] == 0


def test_research_provider_without_key_is_dormant(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(research_mod, "_http_get", lambda *a, **k: called.__setitem__("n", 1))
    assert find_trending("gadgets", settings=_settings(tiktok_research_provider="netrows")) == []
    assert called["n"] == 0


def test_research_parses_provider_results(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"products": [
                {"title": "LED Strip", "price": 19.99, "sales": 5000, "rating": 4.8, "category": "home"},
                {"title": "", "price": 1},  # dropped: no title
            ]}

    monkeypatch.setattr(research_mod, "_http_get", lambda *a, **k: _Resp())
    out = find_trending("home", settings=_settings(tiktok_research_provider="netrows",
                                                   tiktok_research_api_key="k"))
    assert len(out) == 1
    assert out[0].title == "LED Strip"
    assert out[0].sales == 5000


def test_rank_by_opportunity_orders_by_score():
    a = TrendingProduct(title="A", price_usd=20, sales=100, rating=4.9)
    b = TrendingProduct(title="B", price_usd=20, sales=9000, rating=4.0)
    ranked = rank_by_opportunity([a, b])
    assert ranked[0].title == "B"  # far higher sales


# --- campaigns --------------------------------------------------------------

def test_build_caption_has_hashtags():
    cap = build_caption(TrendingProduct(title="Mini Lamp", category="home decor"))
    assert "#tiktokshop" in cap
    assert "#HomeDecor" in cap


def test_build_product_reel_surfaces_reel_failure(monkeypatch):
    from backend.social.video.models import ReelResult

    monkeypatch.setattr(
        "backend.social.video.pipeline.produce_reel",
        lambda *a, **k: ReelResult(ok=False, status="disabled", error="off"),
    )
    res = build_product_reel(TrendingProduct(title="X"), settings=_settings())
    assert res.ok is False
    assert res.status == "disabled"


def test_build_product_reel_success(monkeypatch):
    from backend.social.video.models import ReelResult

    monkeypatch.setattr(
        "backend.social.video.pipeline.produce_reel",
        lambda *a, **k: ReelResult(ok=True, status="ok", mp4_path="/tmp/reel.mp4"),
    )
    res = build_product_reel(TrendingProduct(title="Mini Lamp", category="home"), settings=_settings())
    assert res.ok is True
    assert res.reel_path == "/tmp/reel.mp4"
    assert "Mini Lamp" in res.caption


def test_post_to_tiktok_dry_run_by_default(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(campaigns_mod, "_http_post", lambda *a, **k: called.__setitem__("n", 1))
    result = CampaignResult(ok=True, product_title="X", reel_path="/tmp/r.mp4", caption="c")
    out = post_to_tiktok(result, settings=_settings())
    assert out.dry_run is True
    assert out.posted is False
    assert called["n"] == 0


def test_post_to_tiktok_live_requires_token(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(campaigns_mod, "_http_post", lambda *a, **k: called.__setitem__("n", 1))
    result = CampaignResult(ok=True, product_title="X", reel_path="/tmp/r.mp4", caption="c")
    out = post_to_tiktok(result, settings=_settings(tiktok_dry_run=False,
                                                    tiktok_content_posting_enabled=True))
    assert out.status == "no_access_token"
    assert called["n"] == 0


# --- shop seller (dormant) --------------------------------------------------

def test_seller_disabled_is_dormant():
    assert shop_seller.list_shop_products(settings=_settings())["status"] == "disabled"
    assert shop_seller.fetch_orders(settings=_settings())["status"] == "disabled"
    assert shop_seller.create_listing(title="X", description="d", price_usd=9.99,
                                      settings=_settings())["ok"] is False


def test_seller_enabled_without_token_fails_closed():
    s = _settings(tiktok_shop_enabled=True)
    assert shop_seller.fetch_orders(settings=s)["status"] == "no_access_token"


def test_seller_fetch_orders_attributes_stream(monkeypatch):
    s = _settings(tiktok_shop_enabled=True, tiktok_shop_access_token="tok",
                  revenue_entity_tiktok="Forge Media LLC")

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"orders": [{"id": "o1", "total": 25}, {"id": "o2", "total": 40}]}}

    monkeypatch.setattr(shop_seller, "_http", lambda *a, **k: _Resp())
    report = shop_seller.fetch_orders(settings=s)
    assert report["status"] == "ok"
    assert report["order_count"] == 2
    assert report["entity"] == "Forge Media LLC"
    assert all(o["revenue_stream"] == "tiktok_shop" for o in report["orders"])
