"""Catalog payment-link audit — a revenue guard against dead checkouts.

Stripe payment links get regenerated and archived over time. When a catalog
``payment_link_url`` points at an archived (``active=False``) link, every buy-now
for that SKU lands on a dead checkout and converts ZERO — silently. This was
found the hard way while prepping the 48-Hour Workflow Rescue campaign: 4 links
were stale, including the one the impulse flyer matched every prospect to.

This read-only audit checks each catalog link against the LIVE Stripe payment-
link set and flags any stale one, with the live candidate at the same price, so a
campaign can never launch into a dead funnel again.

Three audit surfaces (all read-only):

1. **Catalog** — ``registry.py`` payment_link_url against live Stripe.
2. **Live code** — ``backend/`` + ``website/src/`` scanned for hardcoded
   ``buy.stripe.com`` URLs that bypass the registry.
3. **WordPress CMS content** — published pages/posts on hustleforge.tech
   fetched via the public WP REST API and scanned for ``buy.stripe.com``
   links that pass through the DOMPurify sanitizer and render clickable.
   This surface is invisible to code-level audits because the HTML lives
   in the WordPress database, not the git tree.

Read-only Stripe: only ``GET /v1/payment_links``. Never creates/archives a link.

CLI::

    python -m backend.catalog.link_audit        # exits 3 if any link is stale
    python -m backend.catalog.link_audit --wp   # also scan WordPress content
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_LOG = logging.getLogger("samus.catalog.link_audit")

_STRIPE_BASE = "https://api.stripe.com"


@dataclass
class StaleLink:
    sku_id: str
    price_usd: float
    current_link: str
    candidates: list[tuple[str, str]] = field(default_factory=list)  # (product, url)


@dataclass
class HardcodedURL:
    """A live-code file that hardcodes a buy.stripe.com URL instead of reading
    it from the catalog. Any hit is a policy violation — the catalog is the
    single source of truth, and a parallel hardcoded copy is what caused the
    ``dRm4gy...``/``cNi5kC...`` incident (see upsell_template.py's history).

    ``stale`` is True when the hardcoded URL is ALSO not in the live active-link
    set (double violation: hardcoded AND dead — customers hit a 404).
    """
    file: str
    line: int
    url: str
    stale: bool


@dataclass(frozen=True)
class LinkDetail:
    """One Stripe payment link with the identity fields remediation needs."""
    url: str
    active: bool
    description: str      # first line item's product description
    amount_cents: int     # first line item's amount_total


def fetch_links_detailed(
    api_key: str,
    *,
    base_url: str = _STRIPE_BASE,
    http_client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> dict[str, LinkDetail]:
    """Return url -> LinkDetail for EVERY payment link, archived included.

    The archived entries are what make deterministic remediation possible: a
    stale URL's (description, amount) identifies its product, and the live
    link with the SAME identity is its rotation successor. Read-only; fail-soft
    like :func:`fetch_active_links`.
    """
    out: dict[str, LinkDetail] = {}
    own = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    headers = {"Authorization": f"Bearer {api_key}"}
    params = [("limit", "100"), ("expand[]", "data.line_items")]
    starting_after: str | None = None
    try:
        for _ in range(20):
            page_params = list(params)
            if starting_after:
                page_params.append(("starting_after", starting_after))
            resp = client.get(f"{base_url}/v1/payment_links",
                              params=page_params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data") or []
            for l in rows:
                li = (l.get("line_items") or {}).get("data") or []
                out[l["url"]] = LinkDetail(
                    url=l["url"],
                    active=bool(l.get("active")),
                    description=(li[0].get("description") or "") if li else "",
                    amount_cents=int(li[0].get("amount_total") or 0) if li else 0,
                )
            if not data.get("has_more") or not rows:
                break
            starting_after = rows[-1]["id"]
    except Exception as exc:  # noqa: BLE001 — read-only guard, never crash
        _LOG.warning("stripe detailed link fetch failed: %s", exc)
    finally:
        if own:
            client.close()
    return out


def fetch_active_links(
    api_key: str,
    *,
    base_url: str = _STRIPE_BASE,
    http_client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> tuple[set[str], dict[int, list[tuple[str, str]]]]:
    """Return (active_url_set, by_price_cents -> [(product, url)]). Read-only.

    Paginates the payment-links endpoint. Fail-soft: on error returns what was
    gathered so the audit degrades to "cannot verify" rather than crashing.
    """
    active: set[str] = set()
    by_price: dict[int, list[tuple[str, str]]] = {}
    own = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    headers = {"Authorization": f"Bearer {api_key}"}
    params = [("limit", "100"), ("expand[]", "data.line_items")]
    starting_after: str | None = None
    try:
        for _ in range(20):  # bound: 20*100 links is far beyond any real account
            page_params = list(params)
            if starting_after:
                page_params.append(("starting_after", starting_after))
            resp = client.get(f"{base_url}/v1/payment_links",
                              params=page_params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data") or []
            for l in rows:
                if not l.get("active"):
                    continue
                active.add(l["url"])
                li = (l.get("line_items") or {}).get("data") or []
                if li:
                    by_price.setdefault(li[0]["amount_total"], []).append(
                        (li[0].get("description") or "", l["url"]))
            if not data.get("has_more") or not rows:
                break
            starting_after = rows[-1]["id"]
    except Exception as exc:  # noqa: BLE001 — read-only guard, never crash
        _LOG.warning("stripe payment-link fetch failed: %s", exc)
    finally:
        if own:
            client.close()
    return active, by_price


def audit_links(
    *,
    api_key: str | None = None,
    active_urls: set[str] | None = None,
    by_price: dict[int, list[tuple[str, str]]] | None = None,
    http_client: httpx.Client | None = None,
    base_url: str = _STRIPE_BASE,
) -> list[StaleLink]:
    """Return the catalog SKUs whose payment_link_url is NOT a live Stripe link.

    When *active_urls* / *by_price* are supplied the Stripe fetch is skipped —
    callers that already called :func:`fetch_active_links` should pass the
    result to avoid a redundant API round-trip.

    Empty list == every catalog checkout is live. Skips SKUs with no link
    (invoiced-manually SKUs). Never raises.
    """
    from backend.catalog.registry import CATALOG

    if active_urls is None or by_price is None:
        key = api_key if api_key is not None else os.getenv("STRIPE_API_KEY", "")
        if not key:
            _LOG.warning("no STRIPE_API_KEY — catalog link audit skipped")
            return []
        active_urls, by_price = fetch_active_links(
            key, base_url=base_url, http_client=http_client,
        )
    if not active_urls:
        _LOG.warning("no active Stripe links fetched — cannot audit")
        return []

    stale: list[StaleLink] = []
    for e in CATALOG:
        link = getattr(e, "payment_link_url", None)
        if not link or link in active_urls:
            continue
        stale.append(StaleLink(
            sku_id=e.sku_id,
            price_usd=e.price_usd_cents / 100.0,
            current_link=link,
            candidates=by_price.get(e.price_usd_cents, []),
        ))
    return stale


# backend/ subtree scanned for hardcoded URLs. registry.py is EXCLUDED because
# it IS the source of truth. Tests, docs, and .data/ artifacts are also skipped
# — the audit only flags live customer-facing code paths.
_SCAN_ROOT = Path(__file__).resolve().parents[2] / "backend"
_SCAN_EXCLUDE_FILES = {
    Path(__file__).resolve().parent / "registry.py",   # source of truth
    Path(__file__).resolve(),                          # this file
}
# Match an ACTUAL Stripe URL (host + path segment), not the bare host string
# used in send_lint.py's URL parser or a docstring mentioning the domain.
_STRIPE_URL_RE = re.compile(r"https?://buy\.stripe\.com/[A-Za-z0-9]+")


def audit_hardcoded_urls(
    *,
    active_urls: set[str] | None = None,
    scan_root: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> list[HardcodedURL]:
    """Scan source trees for files that hardcode a buy.stripe.com URL.

    Scans ``backend/`` by default plus any ``extra_roots`` (e.g. the
    ``website/src/`` tree). Every hit is a violation: the catalog is the
    single source of truth, and parallel hardcoded copies are the exact
    class of bug this module is written to prevent.

    When ``active_urls`` is provided, hits also get a ``stale=True`` flag
    when the URL is not in the live active-link set — those are the
    customer-hits-404 variant, doubly urgent.

    Returns an empty list on a clean tree. Never raises.
    """
    roots = [scan_root or _SCAN_ROOT]
    if extra_roots:
        roots.extend(extra_roots)
    out: list[HardcodedURL] = []
    for root in roots:
        if not root.exists():
            continue
        globs = ["*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.html"]
        for glob in globs:
            for src in root.rglob(glob):
                if src.resolve() in _SCAN_EXCLUDE_FILES:
                    continue
                if "node_modules" in src.parts or ".next" in src.parts:
                    continue
                try:
                    text = src.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    for m in _STRIPE_URL_RE.finditer(line):
                        url = m.group(0)
                        stale = active_urls is not None and url not in active_urls
                        out.append(HardcodedURL(
                            file=str(src.relative_to(root.parent)).replace("\\", "/"),
                            line=lineno,
                            url=url,
                            stale=stale,
                        ))
    return out


# ---- WordPress CMS content scanner -----------------------------------------

_WP_SITE = "hustleforge.tech"
_WP_BASE = f"https://public-api.wordpress.com/wp/v2/sites/{_WP_SITE}"


@dataclass
class WordPressLink:
    """A buy.stripe.com URL found in a published WordPress page or post."""
    post_type: str   # "page" or "post"
    post_id: int
    title: str
    slug: str
    url: str
    stale: bool


def audit_wordpress_content(
    *,
    active_urls: set[str] | None = None,
    http_client: httpx.Client | None = None,
    wp_base: str = _WP_BASE,
    timeout: float = 30.0,
) -> list[WordPressLink]:
    """Fetch published WP pages + posts and scan HTML for buy.stripe.com links.

    The hustleforge.tech site renders WordPress content via
    ``dangerouslySetInnerHTML`` with a DOMPurify sanitizer that allows ``<a>``
    tags. A ``buy.stripe.com`` link in a WP page therefore renders as a live
    clickable checkout link — invisible to code-level audits because it lives
    in the WordPress database, not the git tree.

    Read-only: only ``GET`` against the public WordPress REST API. Fails soft.
    """
    own = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    out: list[WordPressLink] = []
    try:
        for post_type in ("pages", "posts"):
            page = 1
            retries = 0
            while page <= 50:
                resp = client.get(
                    f"{wp_base}/{post_type}",
                    params={"per_page": "100", "page": str(page), "status": "publish"},
                )
                if resp.status_code == 400:
                    break
                if resp.status_code == 429:
                    retries += 1
                    if retries > 3:
                        _LOG.warning("wordpress %s: too many 429s, stopping", post_type)
                        break
                    retry_after = int(resp.headers.get("retry-after", "2"))
                    time.sleep(min(retry_after, 10))
                    continue
                retries = 0
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    break
                for item in items:
                    content = (item.get("content") or {}).get("rendered", "")
                    title_html = (item.get("title") or {}).get("rendered", "")
                    title = re.sub(r"<[^>]+>", "", title_html)
                    slug = item.get("slug", "")
                    post_id = item.get("id", 0)
                    for m in _STRIPE_URL_RE.finditer(content):
                        url = m.group(0)
                        stale = active_urls is not None and url not in active_urls
                        out.append(WordPressLink(
                            post_type=post_type.rstrip("s"),
                            post_id=post_id,
                            title=title,
                            slug=slug,
                            url=url,
                            stale=stale,
                        ))
                page += 1
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("wordpress content scan failed: %s", exc)
    finally:
        if own:
            client.close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.catalog.link_audit",
        description="Flag catalog payment links that no longer point at a live "
                    "Stripe checkout. Read-only. Exit 3 if any are stale.",
    )
    parser.add_argument(
        "--wp", action="store_true",
        help="Also scan WordPress CMS content for buy.stripe.com links "
             "(requires network access to public-api.wordpress.com).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Fetch the live active-link set once so all audits share it.
    key = os.getenv("STRIPE_API_KEY", "")
    active: set[str] | None = None
    if key:
        active, _ = fetch_active_links(key)
        if not active:
            active = None

    stale = audit_links()

    # Scan backend/ + website/src/ for hardcoded URLs.
    website_src = Path(__file__).resolve().parents[2] / ".." / "website" / "src"
    extra: list[Path] = []
    if website_src.exists():
        extra.append(website_src.resolve())
    hardcoded = audit_hardcoded_urls(active_urls=active, extra_roots=extra)

    exit_code = 0

    if stale:
        print(f"STALE catalog payment links: {len(stale)}")
        for s in stale:
            cand = "; ".join(f"{d} -> {u}" for d, u in s.candidates) or "(no live link at this price)"
            print(f"  {s.sku_id} (${s.price_usd:g}) -> live candidate: {cand}")
        print("Fix backend/catalog/registry.py payment_link_url to the live link(s).")
        exit_code = 3
    else:
        print("catalog payment links: all live (0 stale).")

    if hardcoded:
        stale_ct = sum(1 for h in hardcoded if h.stale)
        print(
            f"HARDCODED buy.stripe.com URLs in live code: {len(hardcoded)} "
            f"({stale_ct} stale)",
        )
        for h in hardcoded:
            tag = "STALE" if h.stale else "policy"
            print(f"  [{tag}] {h.file}:{h.line} -> {h.url}")
        print(
            "Every hardcoded URL should be sourced from backend.catalog.registry "
            "via sku_id — a parallel copy is exactly what caused the previous "
            "dead-checkout incident.",
        )
        exit_code = 3
    elif active is not None:
        print("live-code hardcoded URLs: 0 (catalog is the sole source).")

    # WordPress CMS content scan (opt-in — requires network).
    if args.wp:
        wp_links = audit_wordpress_content(active_urls=active)
        if wp_links:
            stale_ct = sum(1 for w in wp_links if w.stale)
            print(
                f"WORDPRESS buy.stripe.com links: {len(wp_links)} "
                f"({stale_ct} stale)",
            )
            for w in wp_links:
                tag = "STALE" if w.stale else "live"
                print(f"  [{tag}] {w.post_type}/{w.slug} (id={w.post_id}) "
                      f"\"{w.title}\" -> {w.url}")
            if stale_ct:
                print(
                    "Stale WordPress links render as clickable buy buttons via "
                    "dangerouslySetInnerHTML — customers reach a dead checkout. "
                    "Update or remove the link in the WP editor.",
                )
            exit_code = 3
        else:
            print("wordpress CMS content: 0 buy.stripe.com links found.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
