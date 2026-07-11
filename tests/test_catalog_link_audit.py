"""Tests for backend.catalog.link_audit — the dead-checkout guard. Stripe mocked."""

from __future__ import annotations

from pathlib import Path

import httpx

from backend.catalog import link_audit
from backend.catalog.registry import CATALOG

_LINKED = [e for e in CATALOG if getattr(e, "payment_link_url", None)]


def _mock(active_entries):
    """active_entries: list of (url, amount_cents). Returns a one-page client."""

    def handler(req: httpx.Request) -> httpx.Response:
        data = [
            {
                "id": f"pl_{i}",
                "url": u,
                "active": True,
                "line_items": {"data": [{"amount_total": amt, "description": "X"}]},
            }
            for i, (u, amt) in enumerate(active_entries)
        ]
        return httpx.Response(200, json={"data": data, "has_more": False})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_audit_all_live_zero_stale():
    active = [(e.payment_link_url, e.price_usd_cents) for e in _LINKED]
    with _mock(active) as c:
        stale = link_audit.audit_links(api_key="sk_test", http_client=c)
    assert stale == []


def test_audit_detects_stale_and_suggests_candidate():
    # Stripe reports a DIFFERENT live link at the same price as seo_audit ($149).
    with _mock([("https://buy.stripe.com/LIVE149", 14900)]) as c:
        stale = link_audit.audit_links(api_key="sk_test", http_client=c)
    skus = {s.sku_id for s in stale}
    assert "seo_audit" in skus  # its catalog link isn't in the active set
    seo = next(s for s in stale if s.sku_id == "seo_audit")
    assert any(u == "https://buy.stripe.com/LIVE149" for _, u in seo.candidates)


def test_audit_no_key_returns_empty():
    assert link_audit.audit_links(api_key="") == []


def test_audit_empty_stripe_cannot_verify_returns_empty():
    # No active links fetched -> degrade to "cannot verify" (empty), not crash.
    with _mock([]) as c:
        assert link_audit.audit_links(api_key="sk_test", http_client=c) == []


# ---- Hardcoded-URL scanner -------------------------------------------------


def test_hardcoded_url_scanner_finds_stripe_urls(tmp_path: Path):
    (tmp_path / "good.py").write_text('url = "https://buy.stripe.com/abc123"\n')
    (tmp_path / "clean.py").write_text('url = "https://example.com"\n')
    hits = link_audit.audit_hardcoded_urls(scan_root=tmp_path)
    assert len(hits) == 1
    assert hits[0].url == "https://buy.stripe.com/abc123"
    assert hits[0].stale is False  # no active_urls provided → default False


def test_hardcoded_url_stale_annotation(tmp_path: Path):
    (tmp_path / "stale.tsx").write_text('href: "https://buy.stripe.com/DEAD",\n')
    active = {"https://buy.stripe.com/LIVE"}
    hits = link_audit.audit_hardcoded_urls(
        scan_root=tmp_path,
        active_urls=active,
    )
    assert len(hits) == 1
    assert hits[0].stale is True


def test_hardcoded_url_skips_node_modules(tmp_path: Path):
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "vendor.js").write_text('x = "https://buy.stripe.com/vendor"\n')
    assert link_audit.audit_hardcoded_urls(scan_root=tmp_path) == []


def test_hardcoded_url_extra_roots(tmp_path: Path):
    root1 = tmp_path / "backend"
    root1.mkdir()
    (root1 / "a.py").write_text('"https://buy.stripe.com/r1"\n')
    root2 = tmp_path / "website"
    root2.mkdir()
    (root2 / "b.tsx").write_text('"https://buy.stripe.com/r2"\n')
    hits = link_audit.audit_hardcoded_urls(
        scan_root=root1,
        extra_roots=[root2],
    )
    urls = {h.url for h in hits}
    assert urls == {
        "https://buy.stripe.com/r1",
        "https://buy.stripe.com/r2",
    }


# ---- WordPress CMS scanner -------------------------------------------------


def _wp_mock(pages_content: list[dict]):
    """Mock the WP REST API. pages_content: list of {title, slug, content, id}."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "/pages" in str(req.url):
            page_num = dict(req.url.params).get("page", "1")
            if page_num == "1":
                data = [
                    {
                        "id": p.get("id", i),
                        "slug": p.get("slug", f"page-{i}"),
                        "title": {"rendered": p.get("title", "")},
                        "content": {"rendered": p.get("content", "")},
                    }
                    for i, p in enumerate(pages_content)
                ]
                return httpx.Response(200, json=data)
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        if "/posts" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_wp_scanner_finds_stripe_links():
    with _wp_mock(
        [
            {
                "title": "SEO Page",
                "slug": "seo",
                "content": '<a href="https://buy.stripe.com/abc123">Buy</a>',
            },
            {"title": "Clean", "slug": "about", "content": "<p>No links</p>"},
        ]
    ) as c:
        hits = link_audit.audit_wordpress_content(http_client=c)
    assert len(hits) == 1
    assert hits[0].slug == "seo"
    assert hits[0].url == "https://buy.stripe.com/abc123"


def test_wp_scanner_stale_annotation():
    active = {"https://buy.stripe.com/LIVE"}
    with _wp_mock(
        [
            {
                "title": "P",
                "slug": "p",
                "content": '<a href="https://buy.stripe.com/DEAD">Buy</a> '
                '<a href="https://buy.stripe.com/LIVE">Buy</a>',
            },
        ]
    ) as c:
        hits = link_audit.audit_wordpress_content(
            active_urls=active,
            http_client=c,
        )
    assert len(hits) == 2
    stale = [h for h in hits if h.stale]
    live = [h for h in hits if not h.stale]
    assert len(stale) == 1
    assert stale[0].url == "https://buy.stripe.com/DEAD"
    assert len(live) == 1
    assert live[0].url == "https://buy.stripe.com/LIVE"


def test_wp_scanner_empty_site():
    with _wp_mock([]) as c:
        assert link_audit.audit_wordpress_content(http_client=c) == []
