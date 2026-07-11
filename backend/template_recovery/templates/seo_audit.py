"""Deterministic SEO-audit scaffold builder.

Pure Python, no I/O, no LLM, constant-time: it fills a fixed structure from
the supplied context. Same input always yields byte-identical output.
"""
from __future__ import annotations

from typing import Any

TEMPLATE_VERSION = "seo_template_v3"


def seo_template_v3(context: dict[str, Any]) -> str:
    """Render a deterministic SEO-audit scaffold from ``context``.

    Recognised context keys (all optional): ``business_name``, ``url``,
    ``target_keywords`` (list[str]), ``industry``.
    """
    business = str(context.get("business_name") or "the business").strip()
    url = str(context.get("url") or "the target site").strip()
    industry = str(context.get("industry") or "general").strip()
    keywords = context.get("target_keywords") or []
    if not isinstance(keywords, (list, tuple)):
        keywords = [str(keywords)]
    kw_lines = "\n".join(
        f"  - {str(kw).strip()}" for kw in keywords
    ) or "  - (no target keywords supplied)"

    return (
        f"# SEO Audit Scaffold — {business}\n"
        f"\n"
        f"Target site: {url}\n"
        f"Industry: {industry}\n"
        f"\n"
        f"## Target keywords\n"
        f"{kw_lines}\n"
        f"\n"
        f"## Findings (deterministic baseline checklist)\n"
        f"  1. Title tag present and within 50-60 characters.\n"
        f"  2. Meta description present and within 140-160 characters.\n"
        f"  3. Single H1 per page; H2/H3 hierarchy intact.\n"
        f"  4. Target keywords present in title, H1 and first paragraph.\n"
        f"  5. All images carry descriptive alt text.\n"
        f"  6. Canonical URL declared; no duplicate-content signals.\n"
        f"  7. XML sitemap present and referenced from robots.txt.\n"
        f"  8. Mobile-responsive viewport meta tag present.\n"
        f"\n"
        f"## Recommended actions\n"
        f"  - Resolve every failing checklist item above in priority order.\n"
        f"  - Re-run the audit once on-page changes ship.\n"
        f"\n"
        f"_Deterministic recovery scaffold ({TEMPLATE_VERSION}). "
        f"Replace with a full LLM audit when budget allows._\n"
    )


__all__ = ["seo_template_v3", "TEMPLATE_VERSION"]
