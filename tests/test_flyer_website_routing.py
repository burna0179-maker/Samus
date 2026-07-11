"""Regression: 'no website' findings must route to the $800 website BUILD, not
the $149 audit (an audit of a non-existent site is irrelevant + under-sells)."""
from __future__ import annotations

from backend.outreach import flyer
from backend.catalog.registry import CATALOG


class _WithLink:
    """Proxy that adds a payment_link_url to the real catalog entry (the live
    catalog has None until the operator creates the Stripe link)."""
    def __init__(self, real, link):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "payment_link_url", link)

    def __getattr__(self, n):
        return getattr(self._real, n)


def _index_with_website_link(monkeypatch, link="https://buy.stripe.com/test_wb"):
    idx = {e.sku_id: e for e in CATALOG}
    idx["service_website_build"] = _WithLink(idx["service_website_build"], link)
    monkeypatch.setattr(flyer, "_catalog_index", lambda: idx)


import pytest


@pytest.mark.parametrize("finding", [
    "no working website",
    "no website coming up at all",
    "the business has no online presence",
    "when someone googles them they are not showing up",
])
def test_no_website_routes_to_build(monkeypatch, finding):
    _index_with_website_link(monkeypatch)
    offer = flyer._matched_offer({"callsheet_finding": finding})
    assert offer is not None
    assert offer.sku_id == "service_website_build"
    assert offer.price_usd == 800.0
    assert "test_wb" in offer.payment_link


def test_site_issue_still_routes_to_audit(monkeypatch):
    # a prospect WITH a site but SEO/security problems still gets the audit
    _index_with_website_link(monkeypatch)
    offer = flyer._matched_offer({"callsheet_finding": "security warning, site graded F"})
    assert offer.sku_id == "seo_audit"


def test_404_still_routes_to_addon(monkeypatch):
    _index_with_website_link(monkeypatch)
    offer = flyer._matched_offer({"callsheet_finding": "several broken 404 links"})
    assert offer.sku_id == "addon_404_audit"
