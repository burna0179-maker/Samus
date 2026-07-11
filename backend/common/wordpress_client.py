"""WordPress.com REST API client — read published pages + create draft pages.

Samus uses this to submit new product/service pages as drafts after a
validated offer surfaces on a call. Alex reviews and publishes from wp-admin.

Auth: WordPress Application Password (Basic Auth).
  Username: env WORDPRESS_USERNAME  (default: Samus)
  Password: env WORDPRESS_APP_PASSWORD

Site: env WORDPRESS_SITE (default: hustleforge.tech)

Two base URLs, on purpose:
  * ``_BASE`` — the WordPress.com proxy
    (``public-api.wordpress.com/wp/v2/sites/{site}``). Serves ANONYMOUS reads
    fine, but it does NOT accept Application-Password Basic auth for
    authenticated calls — it wants a WordPress.com OAuth2 Bearer token, and
    rejects Basic auth with 401 "authentication against the correct blog".
  * ``_AUTH_BASE`` — the site's OWN REST endpoint
    (``https://{site}/wp-json/wp/v2``). This is where Application Passwords
    actually authenticate (the site advertises ``application-passwords`` in its
    /wp-json authentication block). ALL authed calls (whoami, slug lookups with
    context=edit, raw fetch, create/update) MUST use this base.
Read-only public calls stay on ``_BASE``; everything that sends ``_auth_header``
uses ``_AUTH_BASE``.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

_LOG = logging.getLogger("samus.wordpress")

_SITE = os.getenv("WORDPRESS_SITE", "hustleforge.tech")
# Anonymous reads go through the WordPress.com proxy; authenticated calls go to
# the site's own REST endpoint (see module docstring for why they differ).
_BASE = f"https://public-api.wordpress.com/wp/v2/sites/{_SITE}"
_AUTH_BASE = f"https://{_SITE}/wp-json/wp/v2"
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


def _auth_header() -> dict[str, str]:
    """Build Basic Auth header. Credentials are injected from the DPAPI store
    by the PowerShell launcher (Start-MorningDial.ps1 / Create-Opportunity.ps1)
    before the Python process starts — never stored in plaintext."""
    user = os.environ["WORDPRESS_USERNAME"]
    pwd = os.environ["WORDPRESS_APP_PASSWORD"]
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ─── Diagnostics ────────────────────────────────────────────────────────────


def whoami() -> dict[str, Any]:
    """Return the authenticated user WordPress sees for the current credential.

    Purpose-built to disambiguate a write 401: a valid app password that lacks
    page-create capability vs an invalid/revoked password. Hits
    ``GET /users/me?context=edit``, which requires (and echoes) authentication.

    Returns ``{"ok": True, "id", "name", "slug", "roles"}`` on success, or
    ``{"ok": False, "status", "code", "message"}`` on an auth/HTTP failure.
    Never raises — this is a diagnostic, callers branch on ``ok``.
    """
    try:
        r = httpx.get(
            f"{_AUTH_BASE}/users/me",
            params={"context": "edit"},
            headers={"Accept": "application/json", **_auth_header()},
            timeout=_TIMEOUT,
        )
        if r.status_code >= 400:
            body: dict[str, Any] = {}
            try:
                body = r.json()
            except Exception:  # noqa: BLE001 — non-JSON error body
                body = {}
            return {
                "ok": False,
                "status": r.status_code,
                "code": body.get("code", ""),
                "message": body.get("message", r.text[:200]),
            }
        u = r.json()
        return {
            "ok": True,
            "id": u.get("id"),
            "name": u.get("name", ""),
            "slug": u.get("slug", ""),
            "roles": u.get("roles", []),
        }
    except Exception as exc:  # noqa: BLE001 — network/transport
        return {"ok": False, "status": 0, "code": "transport", "message": str(exc)}


# ─── Read ─────────────────────────────────────────────────────────────────────


def get_published_pages() -> list[dict[str, Any]]:
    """Return all published WP pages (id, slug, title, excerpt, content)."""
    try:
        r = httpx.get(
            f"{_BASE}/pages",
            params={"status": "publish", "per_page": 50, "_embed": 1},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        _LOG.warning("wordpress get_published_pages failed: %s", exc)
        return []


def get_page(slug: str) -> dict[str, Any] | None:
    """Fetch a single published page by slug."""
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        return None
    try:
        r = httpx.get(
            f"{_BASE}/pages",
            params={"slug": slug, "status": "publish", "_embed": 1},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        pages = r.json()
        return pages[0] if pages else None
    except Exception as exc:
        _LOG.warning("wordpress get_page(%s) failed: %s", slug, exc)
        return None


def slug_exists(slug: str) -> bool:
    """Return True if a page with this slug exists in any status (publish or draft).

    Used as a SKU gate before creating a new product draft — prevents duplicate
    drafts when the same offer surfaces on multiple calls before Alex publishes.
    Returns False on any API error (fail-open: let the draft through rather than
    silently suppress a genuinely new offer).
    """
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        return False
    try:
        for status in ("publish", "draft"):
            r = httpx.get(
                f"{_AUTH_BASE}/pages",
                params={"slug": slug, "status": status, "per_page": 1},
                headers={"Accept": "application/json", **_auth_header()},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            if r.json():
                return True
        return False
    except Exception as exc:
        _LOG.warning("wordpress slug_exists(%s) failed (fail-open): %s", slug, exc)
        return False


def get_page_any_status(slug: str) -> dict[str, Any] | None:
    """Fetch a page by slug in publish OR draft status, with ``content.raw``.

    ``get_page`` is public and publish-only. This auth'd variant also finds a
    draft Samus created on a prior run, so a re-publish updates that draft in
    place instead of spawning a duplicate. Uses ``context=edit`` so
    ``content.raw`` is present for diffing (round-tripping ``rendered`` would
    strip block markup).

    Returns the first match, or None ONLY when the lookup definitively found no
    such page. Unlike the other readers in this module it does NOT fail-soft to
    None on transport/HTTP errors: a caller deciding create-vs-update must be
    able to tell "confirmed absent" (safe to create) from "couldn't check"
    (creating risks a duplicate). Transport/HTTP errors PROPAGATE — the caller
    decides how to handle an inconclusive check.
    """
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        return None
    for status in ("publish", "draft"):
        r = httpx.get(
            f"{_AUTH_BASE}/pages",
            params={
                "slug": slug,
                "status": status,
                "context": "edit",
                "per_page": 1,
            },
            headers={"Accept": "application/json", **_auth_header()},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        pages = r.json()
        if pages:
            return pages[0]
    return None


def get_page_raw(page_id: int) -> dict[str, Any] | None:
    """Fetch one page with ``context=edit`` so ``content.raw`` is present.

    Editing a page means string-surgery on the RAW (block-editor) content —
    round-tripping the ``rendered`` HTML through an update would strip block
    markup. Requires auth (raw content is not public). Returns None on error.
    """
    try:
        r = httpx.get(
            f"{_AUTH_BASE}/pages/{page_id}",
            params={"context": "edit"},
            headers={"Accept": "application/json", **_auth_header()},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        _LOG.warning("wordpress get_page_raw(%s) failed: %s", page_id, exc)
        return None


def update_page_content(page_id: int, content: str) -> dict[str, Any]:
    """Update ONLY the content of an existing page, preserving its status.

    Used by catalog link remediation to swap an archived buy.stripe.com URL
    for its live successor on an already-published page. Sends nothing but
    ``content`` so title/slug/status stay untouched. Raises RuntimeError on
    failure.
    """
    try:
        r = httpx.post(
            f"{_AUTH_BASE}/pages/{page_id}",
            json={"content": content},
            headers={"Accept": "application/json", **_auth_header()},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        page = r.json()
        _LOG.info("wordpress page content updated: id=%s slug=%s", page.get("id"), page.get("slug"))
        return page
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        raise RuntimeError(f"WordPress update error {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"WordPress update failed: {exc}") from exc


# ─── Write ────────────────────────────────────────────────────────────────────


def create_draft_page(
    title: str,
    content: str,
    excerpt: str = "",
    slug: str = "",
) -> dict[str, Any]:
    """Create a draft WP page and return the created page object.

    The page lands in wp-admin as 'Draft' for Alex to review and publish.
    Raises RuntimeError if the request fails.
    """
    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        "status": "draft",
    }
    if excerpt:
        payload["excerpt"] = excerpt
    if slug:
        payload["slug"] = slug

    try:
        r = httpx.post(
            f"{_AUTH_BASE}/pages",
            json=payload,
            headers={"Accept": "application/json", **_auth_header()},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        page = r.json()
        _LOG.info(
            "wordpress draft created: id=%s slug=%s title=%r",
            page.get("id"),
            page.get("slug"),
            title,
        )
        return page
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        raise RuntimeError(f"WordPress API error {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"WordPress request failed: {exc}") from exc


def update_draft_page(
    page_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    excerpt: str | None = None,
) -> dict[str, Any]:
    """Update an existing draft page (e.g. after a reiteration request)."""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["content"] = content
    if excerpt is not None:
        payload["excerpt"] = excerpt

    try:
        r = httpx.post(
            f"{_AUTH_BASE}/pages/{page_id}",
            json=payload,
            headers={"Accept": "application/json", **_auth_header()},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        raise RuntimeError(f"WordPress update error {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"WordPress update failed: {exc}") from exc
