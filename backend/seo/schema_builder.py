"""schema.org JSON-LD *generation* for the SEO product.

The audit pipeline (``backend.seo.audit``) only *detects* whether a page has
schema markup. This module *produces* valid JSON-LD that the implementation /
retainer deliverable can hand a customer to paste in — the single highest-
leverage GEO win, because FAQPage + Article markup raises featured-snippet
eligibility in Google AND inclusion in AI Overviews at the same time.

All builders are pure functions returning plain dicts. :func:`to_script_tag`
renders one as an embeddable ``<script type="application/ld+json">`` block with
``<`` escaped to ``\\u003c`` (XSS hardening for the customer's HTML).
"""
from __future__ import annotations

import json
from typing import Any

SCHEMA_CONTEXT = "https://schema.org"


def _safe_url(url: str) -> str:
    """WEB-D8-04 — allow only http(s)/mailto/tel and relative URLs in JSON-LD
    logo/image fields; drop everything else (javascript:, data:, vbscript: ...)
    so a poisoned brief URL cannot survive into a consumer's href/src. Returns
    "" when unsafe (``_clean`` then omits the field)."""
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "tel:", "/", "#", "./")):
        return u
    return ""


def _clean(value: Any) -> Any:
    """Drop empty/None values so the emitted JSON-LD stays minimal + valid."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value if v not in (None, "", [], {})]
    return value


def faq_page(faq: list[dict[str, str]]) -> dict[str, Any]:
    """FAQPage from ``[{"q": ..., "a": ...}, ...]``."""
    return _clean(
        {
            "@context": SCHEMA_CONTEXT,
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": (item.get("q") or "").strip(),
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": (item.get("a") or "").strip(),
                    },
                }
                for item in faq
                if (item.get("q") or "").strip() and (item.get("a") or "").strip()
            ],
        }
    )


def article(
    *,
    headline: str,
    description: str = "",
    url: str = "",
    author: str = "",
    publisher: str = "",
    image: str = "",
    date_published: str = "",
    date_modified: str = "",
) -> dict[str, Any]:
    """Article / BlogPosting entity for a content page."""
    image = _safe_url(image)  # WEB-D8-04
    schema: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": "Article",
        "headline": headline.strip()[:110],  # Google truncates >110 chars
        "description": description.strip(),
        "datePublished": date_published,
        "dateModified": date_modified or date_published,
    }
    if image:
        schema["image"] = [image]
    if author:
        schema["author"] = {"@type": "Organization", "name": author}
    if publisher:
        schema["publisher"] = {
            "@type": "Organization",
            "name": publisher,
            "logo": {"@type": "ImageObject", "url": image} if image else None,
        }
    if url:
        schema["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
        schema["url"] = url
    return _clean(schema)


def organization(
    *,
    name: str,
    url: str = "",
    logo: str = "",
    description: str = "",
    same_as: list[str] | None = None,
    telephone: str = "",
    email: str = "",
) -> dict[str, Any]:
    """Organization entity for a homepage / about page."""
    logo = _safe_url(logo)  # WEB-D8-04
    return _clean(
        {
            "@context": SCHEMA_CONTEXT,
            "@type": "Organization",
            "name": name.strip(),
            "url": url,
            "logo": logo,
            "image": logo,
            "description": description.strip(),
            "telephone": telephone,
            "email": email,
            "sameAs": same_as or [],
        }
    )


def local_business(
    *,
    name: str,
    business_type: str = "LocalBusiness",
    url: str = "",
    telephone: str = "",
    street: str = "",
    city: str = "",
    region: str = "",
    postal_code: str = "",
    country: str = "US",
    image: str = "",
    price_range: str = "",
) -> dict[str, Any]:
    """LocalBusiness (or a subtype like Plumber/Dentist) — the highest-value
    markup for the local-SMB prospects Samus audits."""
    image = _safe_url(image)  # WEB-D8-04
    return _clean(
        {
            "@context": SCHEMA_CONTEXT,
            "@type": business_type or "LocalBusiness",
            "name": name.strip(),
            "url": url,
            "telephone": telephone,
            "image": image,
            "priceRange": price_range,
            "address": _clean(
                {
                    "@type": "PostalAddress",
                    "streetAddress": street,
                    "addressLocality": city,
                    "addressRegion": region,
                    "postalCode": postal_code,
                    "addressCountry": country,
                }
            ),
        }
    )


def how_to(*, name: str, steps: list[str], description: str = "") -> dict[str, Any]:
    """HowTo entity for a process / tutorial page. Positions are sequential
    over the non-empty steps (an empty step never leaves a gap)."""
    kept = [s.strip() for s in steps if s and s.strip()]
    return _clean(
        {
            "@context": SCHEMA_CONTEXT,
            "@type": "HowTo",
            "name": name.strip(),
            "description": description.strip(),
            "step": [
                {"@type": "HowToStep", "position": i + 1, "text": s}
                for i, s in enumerate(kept)
            ],
        }
    )


def breadcrumb(items: list[dict[str, str]]) -> dict[str, Any]:
    """BreadcrumbList from ``[{"name": ..., "url": ...}, ...]``."""
    return _clean(
        {
            "@context": SCHEMA_CONTEXT,
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "name": (it.get("name") or "").strip(),
                    "item": it.get("url") or None,
                }
                for i, it in enumerate(items)
                if (it.get("name") or "").strip()
            ],
        }
    )


def to_script_tag(schema: dict[str, Any] | list[dict[str, Any]]) -> str:
    """Render a JSON-LD payload as an embeddable, XSS-hardened script tag."""
    payload = json.dumps(schema, ensure_ascii=False, indent=2).replace("<", "\\u003c")
    return f'<script type="application/ld+json">\n{payload}\n</script>'


# ---------------------------------------------------------------------------
# GEO / AI-citation additions (2026-06-11)
# ---------------------------------------------------------------------------

# AI search crawler user-agents that should be allowed in robots.txt.
_AI_CRAWLERS: tuple[str, ...] = (
    "OAI-SearchBot",
    "ChatGPT-User",
    "PerplexityBot",
    "Claude-SearchBot",
    "Claude-User",
)


def build_robots_txt_ai_block() -> str:
    """Generate a paste-ready robots.txt snippet that allows all major AI
    search crawlers to index the site.

    Returns a multi-line ASCII string. Each crawler gets its own
    User-agent / Allow stanza so WAFs that process rules per-agent
    don't silently collapse them.

    Example output::

        # AI search crawler access (GEO)
        User-agent: OAI-SearchBot
        Allow: /

        User-agent: ChatGPT-User
        Allow: /
        ...
    """
    lines = ["# AI search crawler access (GEO)"]
    for bot in _AI_CRAWLERS:
        lines.append(f"User-agent: {bot}")
        lines.append("Allow: /")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_article_schema(
    *,
    headline: str,
    description: str = "",
    url: str = "",
    author_name: str = "",
    publisher_name: str = "",
    publisher_logo: str = "",
    image: str = "",
    date_published: str = "",
    date_modified: str = "",
) -> dict[str, Any]:
    """Article JSON-LD with author, datePublished, and dateModified.

    This is a convenience wrapper around :func:`article` that:
      - Uses Person @type for the author (more appropriate for blog bylines)
      - Ensures dateModified is never omitted (defaults to datePublished)
      - Accepts publisher_logo separately for the ImageObject

    All AI citation engines treat dateModified as a freshness signal;
    omitting it causes content to be ranked lower than explicitly fresh
    alternatives.
    """
    image_url = _safe_url(image)
    pub_logo_url = _safe_url(publisher_logo)

    schema: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": "Article",
        "headline": headline.strip()[:110],
        "description": description.strip(),
        "datePublished": date_published,
        "dateModified": date_modified or date_published,
    }
    if image_url:
        schema["image"] = [image_url]
    if author_name:
        schema["author"] = {"@type": "Person", "name": author_name.strip()}
    if publisher_name:
        logo_obj: dict[str, Any] = {"@type": "ImageObject", "url": pub_logo_url} if pub_logo_url else {}
        schema["publisher"] = _clean({
            "@type": "Organization",
            "name": publisher_name.strip(),
            "logo": logo_obj or None,
        })
    if url:
        schema["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
        schema["url"] = url
    return _clean(schema)
