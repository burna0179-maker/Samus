"""WordPress page tools — Samus submits validated offers as draft pages.

Called after a call ends and a product is confirmed as selling well.
The draft appears in hustleforge.tech/wp-admin for Alex to review and publish.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.common.wordpress_client import create_draft_page, slug_exists, update_draft_page

_LOG = logging.getLogger("samus.tools.wordpress_pages")


def _slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def submit_product_page(
    product_name: str,
    description: str,
    price_usd: str,
    key_deliverables: list[str],
    call_context: str = "",
) -> dict[str, Any]:
    """Build and submit a draft product page from a validated sales call.

    Args:
        product_name: Human-readable product title (e.g. "48-Hour Workflow Rescue")
        description: 1-3 sentence product description
        price_usd: Price string (e.g. "$1,500")
        key_deliverables: Bullet list of what the customer receives
        call_context: Optional note about the call where this was validated

    Returns:
        dict with 'page_id', 'slug', 'edit_url', 'status'
    """
    deliverables_html = "\n".join(f"<li>{item}</li>" for item in key_deliverables)

    context_block = (
        f"<p><em>Added from sales call: {call_context}</em></p>\n" if call_context else ""
    )

    content = f"""<!-- wp:paragraph -->
<p>{description}</p>
<!-- /wp:paragraph -->

<!-- wp:heading -->
<h2>What You Get</h2>
<!-- /wp:heading -->

<!-- wp:list -->
<ul>
{deliverables_html}
</ul>
<!-- /wp:list -->

<!-- wp:paragraph -->
<p><strong>Investment:</strong> {price_usd}</p>
<!-- /wp:paragraph -->

{context_block}"""

    excerpt = description[:200] if len(description) > 200 else description
    slug = _slugify(product_name)

    # SKU gate: if a page with this slug already exists (published or draft),
    # this offer is already in the catalogue — skip silently rather than
    # creating duplicate drafts every time the same offer lands on a call.
    if slug_exists(slug):
        _LOG.info("product page skipped (SKU already exists): %s (slug=%s)", product_name, slug)
        return {
            "status": "skipped_existing_sku",
            "slug": slug,
            "message": f"'{product_name}' already exists in WordPress (slug={slug}). No draft created.",
        }

    try:
        page = create_draft_page(
            title=product_name,
            content=content,
            excerpt=excerpt,
            slug=slug,
        )
        page_id = page.get("id")
        page_slug = page.get("slug", slug)
        edit_url = f"https://hustleforge.tech/wp-admin/post.php?post={page_id}&action=edit"

        _LOG.info("product page draft submitted: %s (id=%s)", product_name, page_id)

        return {
            "status": "draft_created",
            "page_id": page_id,
            "slug": page_slug,
            "edit_url": edit_url,
            "message": (
                f"Draft page '{product_name}' created. Alex can review and publish at: {edit_url}"
            ),
        }
    except RuntimeError as exc:
        _LOG.error("failed to submit product page '%s': %s", product_name, exc)
        return {
            "status": "error",
            "message": str(exc),
        }


def revise_product_page(
    page_id: int,
    product_name: str | None = None,
    description: str | None = None,
    price_usd: str | None = None,
    key_deliverables: list[str] | None = None,
) -> dict[str, Any]:
    """Update an existing draft after a reiteration request from Alex."""
    try:
        payload: dict[str, Any] = {}

        if product_name:
            payload["title"] = product_name

        if description or price_usd or key_deliverables:
            parts = []
            if description:
                parts.append(f"<!-- wp:paragraph -->\n<p>{description}</p>\n<!-- /wp:paragraph -->")
            if key_deliverables:
                items = "\n".join(f"<li>{i}</li>" for i in key_deliverables)
                parts.append(f"<!-- wp:list -->\n<ul>\n{items}\n</ul>\n<!-- /wp:list -->")
            if price_usd:
                parts.append(
                    f"<!-- wp:paragraph -->\n<p><strong>Investment:</strong> {price_usd}</p>\n<!-- /wp:paragraph -->"
                )
            if parts:
                payload["content"] = "\n\n".join(parts)

        page = update_draft_page(page_id, **payload)
        edit_url = f"https://hustleforge.tech/wp-admin/post.php?post={page_id}&action=edit"

        return {
            "status": "draft_updated",
            "page_id": page_id,
            "edit_url": edit_url,
            "message": f"Draft page {page_id} updated. Review at: {edit_url}",
        }
    except RuntimeError as exc:
        _LOG.error("failed to revise page %s: %s", page_id, exc)
        return {"status": "error", "message": str(exc)}
