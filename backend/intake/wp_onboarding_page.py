"""Publish the WordPress /onboarding fallback page — Samus owns its funnel.

The Next.js site (hustleforge.ai) serves the primary ``/onboarding`` form.
This module ships the *fallback* the 2026-07-03 funnel audit called for: a
self-contained WordPress page at ``hustleforge.tech/onboarding`` that renders
the SAME form against the SAME Samus intake API, so a visitor still has a
working purchase/lead path if Next.js routing lags or the hustleforge.ai
deploy is unreachable.

The page body is a packaged asset (``onboarding_fallback.html``) — no external
CSS/JS, fetches ``/intake/form-schema``, POSTs to ``/intake/onboarding``, and
beacons ``/intake/telemetry`` exactly like the Next.js page. Editing the form
means editing that HTML, not this module.

Safety posture (mirrors ``backend.catalog.wp_link_remediation``):

  * WIRED-DORMANT — applying requires ``SAMUS_WP_ONBOARDING_PUBLISH_ENABLED=1``;
    unarmed, ``apply_publish()`` refuses and the CLI prints the plan only.
  * Dry-run by default — the CLI needs an explicit ``--apply``.
  * Creates a DRAFT — a brand-new page lands in wp-admin as 'Draft' for review
    before it goes live (same as ``create_draft_page`` everywhere else). An
    already-existing page (publish or draft) is updated in place, preserving
    its status, so publishing is a one-click human step and re-runs never
    change visibility.
  * Idempotent — if the live page's raw content already matches the asset, the
    plan is a no-op and nothing is written.

CLI::

    python -m backend.intake.wp_onboarding_page            # plan only
    python -m backend.intake.wp_onboarding_page --apply     # enact (if armed)
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("samus.intake.wp_onboarding_page")

# The wp-admin slug this page lives at: hustleforge.tech/onboarding.
ONBOARDING_SLUG = "onboarding"
ONBOARDING_TITLE = "Onboarding"

# Packaged next to this module. The website repo keeps a human-editable
# reference copy at website/scripts/wp-onboarding-fallback.html; THIS file is
# the shipped canonical the publisher reads (no cross-repo runtime dependency).
_ASSET_PATH = Path(__file__).with_name("onboarding_fallback.html")


def _publish_armed() -> bool:
    """WIRE vs ARM: WordPress writes are off unless the operator arms them."""
    return os.getenv("SAMUS_WP_ONBOARDING_PUBLISH_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def load_fallback_html() -> str:
    """Return the packaged onboarding page body. Raises if the asset is missing
    (a missing asset is a packaging bug, not a runtime condition to swallow)."""
    return _ASSET_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True)
class PublishPlan:
    """What publishing would do, computed read-only before any write."""

    action: str  # "create" | "update" | "noop" | "blocked"
    slug: str
    page_id: Optional[int]
    status: str  # existing page status, or "" when absent
    reason: str
    content_len: int


@dataclass
class PublishResult:
    action: str = ""  # what was actually done: "create" | "update" | ""
    page_id: Optional[int] = None
    skipped_reason: str = ""
    error: str = ""


def plan_publish(*, html: Optional[str] = None) -> PublishPlan:
    """Read-only pass: decide create vs update vs noop for the fallback page.

    The existence check must be conclusive before we can plan ``create``: a
    transient 429 / 401 / network error during the lookup must NOT be read as
    "page absent", or a retry could spawn a duplicate. So an errored lookup
    yields ``action="blocked"`` (apply refuses it) rather than a create.
    """
    body = html if html is not None else load_fallback_html()
    from backend.common import wordpress_client as wp

    try:
        existing = wp.get_page_any_status(ONBOARDING_SLUG)
    except Exception as exc:  # noqa: BLE001 — inconclusive check, not a crash
        return PublishPlan(
            action="blocked",
            slug=ONBOARDING_SLUG,
            page_id=None,
            status="",
            reason=(
                f"existence check failed ({exc}) — refusing to create to "
                f"avoid a duplicate; fix creds/permissions and retry"
            ),
            content_len=len(body),
        )
    if not existing:
        return PublishPlan(
            action="create",
            slug=ONBOARDING_SLUG,
            page_id=None,
            status="",
            reason="no publish/draft page at this slug — will create a draft",
            content_len=len(body),
        )

    page_id = existing.get("id")
    status = existing.get("status", "") or ""
    raw = ((existing.get("content") or {}).get("raw", "")) or ""
    if raw == body:
        return PublishPlan(
            action="noop",
            slug=ONBOARDING_SLUG,
            page_id=page_id,
            status=status,
            reason="live page content already matches the asset — nothing to do",
            content_len=len(body),
        )
    return PublishPlan(
        action="update",
        slug=ONBOARDING_SLUG,
        page_id=page_id,
        status=status,
        reason=f"page exists ({status}) with differing content — will update in place",
        content_len=len(body),
    )


def apply_publish(
    plan: PublishPlan,
    *,
    html: Optional[str] = None,
    dry_run: bool = True,
    force_armed: Optional[bool] = None,
) -> PublishResult:
    """Enact the plan through Samus's WordPress credential.

    Refuses (skipped_reason set) when the arm flag is off, ``dry_run`` is True,
    the plan is a no-op, or the plan is ``blocked`` (existence check was
    inconclusive — creating could duplicate). Never raises — a write failure is
    captured in ``error``.
    """
    result = PublishResult()
    armed = _publish_armed() if force_armed is None else force_armed
    if not armed:
        result.skipped_reason = (
            "SAMUS_WP_ONBOARDING_PUBLISH_ENABLED is off — wired-dormant, plan only"
        )
        return result
    if dry_run:
        result.skipped_reason = "dry_run — pass dry_run=False (--apply) to enact"
        return result
    if plan.action == "blocked":
        result.skipped_reason = plan.reason
        return result
    if plan.action == "noop":
        result.skipped_reason = plan.reason
        return result

    body = html if html is not None else load_fallback_html()
    from backend.common import wordpress_client as wp

    try:
        if plan.action == "create":
            page = wp.create_draft_page(
                title=ONBOARDING_TITLE,
                content=body,
                slug=ONBOARDING_SLUG,
            )
            result.action = "create"
            result.page_id = page.get("id")
            _LOG.info("wp onboarding page created as draft: id=%s", result.page_id)
        elif plan.action == "update":
            if plan.page_id is None:
                result.error = "update planned but page_id is unknown"
                return result
            page = wp.update_page_content(plan.page_id, body)
            result.action = "update"
            result.page_id = page.get("id")
            _LOG.info("wp onboarding page updated in place: id=%s", result.page_id)
        else:
            result.skipped_reason = f"unknown plan action {plan.action!r}"
    except Exception as exc:  # noqa: BLE001 — surface, never crash the caller
        result.error = str(exc)
        _LOG.warning("wp onboarding publish failed: %s", exc)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    """Plan (default) or publish the WP onboarding fallback page.

    Exit codes: 0 = success / nothing to do; 1 = write failed.
    """
    parser = argparse.ArgumentParser(
        prog="python -m backend.intake.wp_onboarding_page",
        description="Publish the hustleforge.tech/onboarding fallback page. "
        "Plan-only by default; --apply enacts (requires "
        "SAMUS_WP_ONBOARDING_PUBLISH_ENABLED=1).",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Enact the publish (armed installs only)."
    )
    parser.add_argument(
        "--whoami",
        action="store_true",
        help="Print the WP user the current credential "
        "authenticates as (roles included), then exit. "
        "Use this to diagnose a write 401: invalid "
        "password vs a role lacking page-create rights.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.whoami:
        from backend.common import wordpress_client as wp

        info = wp.whoami()
        if info.get("ok"):
            roles = ", ".join(info.get("roles") or []) or "(none)"
            print(
                f"authenticated as: {info.get('slug')} "
                f"(id={info.get('id')}, name={info.get('name')!r})"
            )
            print(f"  roles: {roles}")
            can_pages = any(r in ("administrator", "editor") for r in (info.get("roles") or []))
            if can_pages:
                print(
                    "  -> role can create/edit pages. A write 401 now points "
                    "elsewhere (recheck the app password value)."
                )
            else:
                print(
                    "  -> role CANNOT create/edit pages (needs editor or "
                    "administrator). This is the write-401 cause — raise the "
                    "Samus user's role in wp-admin."
                )
            return 0
        status = info.get("status")
        code = (info.get("code") or "").lower()
        print(f"whoami FAILED: HTTP {status} {info.get('code')} — {info.get('message')}")
        # Cause-specific guidance — a 401 has several distinct causes and only
        # some of them are the app password. Don't blame the password blindly.
        if status == 429:
            print(
                "  -> RATE-LIMITED, not an auth failure. Wait a few minutes "
                "and retry; nothing to change."
            )
        elif status == 0 or code == "transport":
            print(
                "  -> network/transport error reaching WordPress — not an "
                "auth verdict. Check connectivity and retry."
            )
        elif "incorrect_password" in code or "application_password" in code:
            print(
                "  -> WordPress received the app password and REJECTED it: "
                "it's wrong/revoked, or app passwords are disabled for this "
                "user. Regenerate at wp-admin -> Profile -> Application "
                "Passwords and reseal (Set-HfSecret -Scope Samus -Name "
                "WordPressAppPassword)."
            )
        elif code == "rest_not_logged_in" or status == 401:
            print(
                "  -> WordPress saw NO valid credentials. This is usually one "
                "of, in order of likelihood:"
            )
            print(
                "     1. USERNAME mismatch — WORDPRESS_USERNAME must be the WP "
                "*login*, not the display name. Confirm the exact login in "
                "wp-admin -> Users, then reseal WordPressUsername."
            )
            print(
                "     2. Stale/malformed app password — regenerate it and "
                "reseal WordPressAppPassword."
            )
            print(
                "     3. The server is stripping the Authorization header "
                "(managed-WP quirk) — only if 1 & 2 are confirmed good."
            )
        else:
            print(
                "  -> the credential did not authenticate. Verify the WP "
                "username + app password, then reseal into DPAPI."
            )
        return 1

    try:
        body = load_fallback_html()
    except OSError as exc:
        print(f"cannot read onboarding asset: {exc}")
        return 1

    plan = plan_publish(html=body)
    print(f"wp onboarding page plan: {plan.action} (slug={plan.slug}, {plan.content_len} bytes)")
    print(f"  {plan.reason}")

    # A blocked plan means the existence check itself failed (bad creds, WP
    # permission gap, rate-limit, network). Nothing can be enacted and the
    # non-zero exit lets a scheduled run surface it instead of looking clean.
    if plan.action == "blocked":
        return 1

    if not args.apply:
        if plan.action != "noop":
            print(
                "  (plan only — pass --apply with SAMUS_WP_ONBOARDING_PUBLISH_ENABLED=1 to enact)"
            )
        return 0

    result = apply_publish(plan, html=body, dry_run=False)
    if result.skipped_reason:
        print(f"apply skipped: {result.skipped_reason}")
        return 0
    if result.error:
        print(f"apply FAILED: {result.error}")
        return 1
    print(f"applied: {result.action} (page id={result.page_id})")
    if result.action == "create":
        print("  page created as DRAFT — publish it from wp-admin to go live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
