"""WordPress checkout-link remediation — Samus fixes its own dead funnel.

The funnel-health snapshot can tell Samus a WordPress page carries an archived
buy.stripe.com link, but the fix used to be a manual wp-admin edit. Samus has
write access to WordPress (application password, the same credential the
draft-page flow uses), so this module closes the loop: PLAN the deterministic
replacements, then APPLY them through the WP REST API.

Determinism contract — a stale link is auto-fixable ONLY when Stripe itself
names its successor:

  * The archived link's line item gives (product description, amount).
  * Exactly ONE live link has the same (description, amount).
  * Then the live link is the rotation successor — the same product at the
    same price, re-issued. Replace the URL string in the page's RAW content.

Anything else (``buy.stripe.com/test`` placeholders, a product with zero or
multiple live candidates, a link Stripe has no record of) is reported as
MANUAL — Samus never guesses what a human meant a link to sell.

Safety posture (mirrors the funnel gate):

  * WIRED-DORMANT — applying requires ``SAMUS_WP_REMEDIATION_ENABLED=1``;
    unarmed, apply() refuses and the CLI prints the plan only.
  * Dry-run by default — the CLI needs an explicit ``--apply``.
  * Surgical writes — only the stale URL, matched at a token boundary (so a
    longer live link sharing its prefix is never corrupted), is replaced in the
    page's raw content; title, slug, status, and everything else untouched.
  * Idempotent — re-running after a successful pass finds no stale links and
    plans nothing.

CLI::

    python -m backend.catalog.wp_link_remediation            # plan only
    python -m backend.catalog.wp_link_remediation --apply    # enact (if armed)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

_LOG = logging.getLogger("samus.catalog.wp_link_remediation")


def _boundary_replace(text: str, stale_url: str, live_url: str) -> tuple[str, int]:
    """Replace ``stale_url`` with ``live_url`` only at a Stripe-token boundary.

    A stale link is a maximal ``buy.stripe.com/<alnum>`` token. A plain
    ``str.replace`` would also rewrite a *different, live* link whose token has
    the stale one as a prefix — e.g. replacing ``…/abc`` inside ``…/abcXYZ``
    turns a good checkout link into ``{live_url}XYZ`` on the live page. A
    negative lookahead for a trailing alphanumeric pins the match to the whole
    token so only genuine occurrences of the stale link are rewritten. The
    replacement is applied via a function so ``live_url`` is treated literally
    (no regex backreference expansion). Returns ``(new_text, count)``.
    """
    pattern = re.escape(stale_url) + r"(?![A-Za-z0-9])"
    return re.subn(pattern, lambda _m: live_url, text)


def _remediation_armed() -> bool:
    """WIRE vs ARM: WordPress writes are off unless the operator arms them."""
    return os.getenv("SAMUS_WP_REMEDIATION_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass(frozen=True)
class LinkFix:
    """One planned replacement on one WordPress page."""
    page_id: int
    post_type: str
    slug: str
    title: str
    stale_url: str
    live_url: str          # empty when action == "manual"
    action: str            # "replace" | "manual"
    reason: str


@dataclass
class RemediationPlan:
    fixes: list[LinkFix] = field(default_factory=list)

    @property
    def auto(self) -> list[LinkFix]:
        return [f for f in self.fixes if f.action == "replace"]

    @property
    def manual(self) -> list[LinkFix]:
        return [f for f in self.fixes if f.action == "manual"]


@dataclass
class ApplyResult:
    applied: list[LinkFix] = field(default_factory=list)
    failed: list[tuple[LinkFix, str]] = field(default_factory=list)
    skipped_reason: str = ""


def _successor(stale_url: str, details: dict) -> tuple[str, str]:
    """Resolve a stale URL's live successor. Returns (live_url, reason).

    Empty live_url means the fix is manual; reason says why.
    """
    d = details.get(stale_url)
    if d is None:
        return "", "stripe has no record of this link (placeholder/test?)"
    if d.active:
        return "", "link is actually live - nothing to fix"
    if not d.description:
        return "", "archived link has no line item to match a successor by"
    candidates = [
        x for x in details.values()
        if x.active and x.description == d.description
        and x.amount_cents == d.amount_cents
    ]
    if len(candidates) == 1:
        return candidates[0].url, (
            f"archived '{d.description}' (${d.amount_cents / 100:g}) has "
            f"exactly one live successor"
        )
    if not candidates:
        return "", (f"no live link for '{d.description}' at "
                    f"${d.amount_cents / 100:g} - product may be retired")
    return "", (f"{len(candidates)} live links match '{d.description}' at "
                f"${d.amount_cents / 100:g} - ambiguous, operator must pick")


def plan_remediations(
    *,
    api_key: Optional[str] = None,
    http_client=None,
) -> RemediationPlan:
    """Read-only pass: find stale WP links and resolve each to a fix or MANUAL.

    Never raises; no Stripe key or empty fetches yield an empty plan (the
    funnel-health surface already reports the unverified state).
    """
    from backend.catalog import link_audit

    key = api_key if api_key is not None else os.getenv("STRIPE_API_KEY", "")
    plan = RemediationPlan()
    if not key:
        _LOG.warning("no STRIPE_API_KEY - cannot plan WP remediation")
        return plan

    details = link_audit.fetch_links_detailed(key, http_client=http_client)
    if not details:
        _LOG.warning("no Stripe links fetched - cannot plan WP remediation")
        return plan
    active = {u for u, d in details.items() if d.active}

    wp_links = link_audit.audit_wordpress_content(
        active_urls=active, http_client=http_client,
    )
    seen: set[tuple[int, str]] = set()
    for w in wp_links:
        if not w.stale:
            continue
        dedup_key = (w.post_id, w.url)
        if dedup_key in seen:   # same URL repeated on one page = one string fix
            continue
        seen.add(dedup_key)
        live, reason = _successor(w.url, details)
        plan.fixes.append(LinkFix(
            page_id=w.post_id, post_type=w.post_type, slug=w.slug,
            title=w.title, stale_url=w.url, live_url=live,
            action="replace" if live else "manual", reason=reason,
        ))
    return plan


def apply_remediations(
    plan: RemediationPlan,
    *,
    dry_run: bool = True,
    force_armed: Optional[bool] = None,
) -> ApplyResult:
    """Enact the plan's auto-fixes through Samus's WordPress credential.

    Refuses entirely (skipped_reason set) when the arm flag is off or
    ``dry_run`` is True. Per-page failures are collected, never raised — one
    broken page must not abort the rest of the pass.
    """
    result = ApplyResult()
    armed = _remediation_armed() if force_armed is None else force_armed
    if not armed:
        result.skipped_reason = (
            "SAMUS_WP_REMEDIATION_ENABLED is off - wired-dormant, plan only"
        )
        return result
    if dry_run:
        result.skipped_reason = "dry_run - pass dry_run=False (--apply) to enact"
        return result

    from backend.common import wordpress_client as wp

    # Group by page so a page with several stale links gets ONE read + write.
    by_page: dict[int, list[LinkFix]] = {}
    for f in plan.auto:
        by_page.setdefault(f.page_id, []).append(f)

    for page_id, fixes in by_page.items():
        try:
            page = wp.get_page_raw(page_id)
            raw = ((page or {}).get("content") or {}).get("raw", "")
            if not raw:
                for f in fixes:
                    result.failed.append((f, "raw content unavailable"))
                continue
            new_raw = raw
            page_applied: list[LinkFix] = []
            for f in fixes:
                # Boundary-aware: a bare ``str.replace`` would corrupt a longer
                # live link that has this stale token as a prefix. ``count == 0``
                # means the stale link is absent OR present only as such a prefix
                # — either way, do not write.
                new_raw, count = _boundary_replace(new_raw, f.stale_url, f.live_url)
                if count == 0:
                    result.failed.append((f, "stale url not found at a token boundary"))
                    continue
                page_applied.append(f)
            if not page_applied:
                continue
            if new_raw == raw:
                for f in page_applied:
                    result.failed.append((f, "no-op replacement"))
                continue
            wp.update_page_content(page_id, new_raw)
            result.applied.extend(page_applied)
            for f in page_applied:
                _LOG.info("wp remediation applied: %s/%s %s -> %s",
                          f.post_type, f.slug, f.stale_url, f.live_url)
        except Exception as exc:  # noqa: BLE001 — isolate per-page failures
            for f in fixes:
                if f not in result.applied:
                    result.failed.append((f, str(exc)))
    return result


def main(argv: Optional[list[str]] = None) -> int:
    """Plan (default) or apply WP link fixes. Exit 3 if manual fixes remain."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.catalog.wp_link_remediation",
        description="Replace archived buy.stripe.com links on WordPress pages "
                    "with their live Stripe successors. Plan-only by default; "
                    "--apply enacts (requires SAMUS_WP_REMEDIATION_ENABLED=1).",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Enact the auto-fixes (armed installs only).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    plan = plan_remediations()
    if not plan.fixes:
        print("wp link remediation: nothing to fix.")
        return 0

    print(f"wp link remediation plan: {len(plan.auto)} auto, "
          f"{len(plan.manual)} manual")
    for f in plan.auto:
        print(f"  [auto]   {f.post_type}/{f.slug}: {f.stale_url}")
        print(f"           -> {f.live_url}  ({f.reason})")
    for f in plan.manual:
        print(f"  [manual] {f.post_type}/{f.slug}: {f.stale_url}")
        print(f"           {f.reason}")

    if args.apply:
        result = apply_remediations(plan, dry_run=False)
        if result.skipped_reason:
            print(f"apply skipped: {result.skipped_reason}")
        else:
            print(f"applied {len(result.applied)}, failed {len(result.failed)}")
            for f, err in result.failed:
                print(f"  FAILED {f.post_type}/{f.slug} {f.stale_url}: {err}")
            if result.applied:
                # The funnel snapshot is now stale by construction - refresh it
                # so the gate and the brief see the fixed state immediately.
                try:
                    from backend.catalog.funnel_health import refresh_snapshot
                    snap = refresh_snapshot(include_wp=True)
                    print(f"funnel snapshot refreshed: {snap.summary_line()}")
                except Exception as exc:  # noqa: BLE001
                    print(f"funnel snapshot refresh failed: {exc}")

    return 3 if plan.manual else 0


if __name__ == "__main__":
    raise SystemExit(main())
