"""Per-touch email templates for the audit-to-monthly upsell sequence.

Three touches per source-offer-code, each with a distinct angle so the
sequence sounds like a single human nurturing rather than three copies of
the same pitch:

  - Touch 1 (D+5)   — "report landed, here's what fixing it looks like"
  - Touch 2 (D+12)  — "checking in — pattern we see in similar audits"
  - Touch 3 (D+30)  — "door's still open, last touch from me"

Today only the ``seo_audit`` source-offer-code has templates (operator
confirmed https://hustleforge.tech/seo-optimization/ is live + the matching
Stripe payment link). Add a new mapping in ``upsell_queue.UPSELL_TARGET_MAP``
+ a new entry below to support additional source offers.

Each template returns ``(subject, text_body, html_body, payment_link_url)``.
The runner sends via ``backend.common.email_backend.send_email`` — same
adapter the payment receipt + auto-fulfill chain use.

Style notes (matches the rest of the receipts.py family):
  - Plain text body first, HTML mirrors it block-for-block
  - Use a public-page link (hustleforge.tech/...) in the body as a trust
    corroboration step alongside the buy link — so the prospect can
    eyeball the offer before clicking "pay"
  - Sign as a person, not a brand voice — Morgan @ HustleForge
"""
from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import urlencode, urlparse


_LOG = logging.getLogger("samus.finance.upsell_template")


def _append_query_param(url: str, key: str, value: str) -> str:
    """Append ``?key=value`` (or ``&key=value`` if a query string exists).

    Cheap implementation — does NOT re-encode existing params or merge
    duplicate keys. Sufficient for Stripe payment-link URLs (which we
    publish as bare ``https://buy.stripe.com/<id>`` with no query string).
    """
    if not url:
        return url
    encoded = urlencode({key: value})
    sep = "&" if urlparse(url).query else "?"
    return f"{url}{sep}{encoded}"


# Public landing pages that corroborate the buy link. Keeping these in one
# place so the operator can rotate them without editing email bodies.
_PUBLIC_PAGE_URL_BY_TARGET: dict[str, str] = {
    "seo_implementation": "https://hustleforge.tech/seo-implementation/",
    "seo_optimization": "https://hustleforge.tech/seo-optimization/",
    "service_workflow_buildout": "https://hustleforge.tech/workflow-system-buildout/",
    "service_ai_ops_partner_build": "https://hustleforge.tech/ai-ops-partner/",
}


# Mapping of upsell target_offer_code -> catalog sku_id. The buy URL itself
# is read live from ``backend.catalog.registry.CATALOG`` at send time so it
# stays in sync with the catalog's single source of truth. Prior versions of
# this module carried a hardcoded parallel copy of buy URLs that repeatedly
# went stale when Stripe archived a payment link (dRm4gy... and cNi5kC...
# both went dead once); sourcing from the catalog makes that class of drift
# impossible. Quote-based targets (workflow buildout / AI ops partner build)
# have no catalog payment_link_url — _payment_link() returns "" for them
# and the composer uses a "reply to scope" CTA instead.
_TARGET_SKU_MAP: dict[str, str] = {
    "seo_implementation": "service_seo_implementation",
    "seo_optimization": "retainer_seo_optimization",
}


# Friendly display names for subject lines + body copy.
_TARGET_DISPLAY_NAME: dict[str, str] = {
    "seo_implementation": "SEO Implementation",
    "seo_optimization": "SEO Optimization",
    "service_workflow_buildout": "Workflow System Buildout",
    "service_ai_ops_partner_build": "AI Ops Partner — Build",
}


# One-time price for fix-tier targets (seo_implementation = $200 flat).
_TARGET_ONE_TIME_USD: dict[str, int] = {
    "seo_implementation": 200,
}


# Monthly price for retainer-tier targets.
_TARGET_MONTHLY_USD: dict[str, int] = {
    "seo_optimization": 300,
}


# Quote-based pricing range (low, high) for human-in-the-loop tiers — used in
# composer copy to give the customer a real number to anchor on without
# committing to a fixed price the operator may need to adjust during scoping.
_TARGET_QUOTE_RANGE_USD: dict[str, tuple[int, int]] = {
    "service_workflow_buildout": (2500, 3000),
    "service_ai_ops_partner_build": (2000, 5000),
}


def _catalog_payment_link(target_offer_code: str) -> str:
    """Live catalog lookup for the target's Stripe payment link.

    Returns the ``payment_link_url`` from the catalog entry whose ``sku_id``
    maps to ``target_offer_code``. Returns "" when: (a) the target has no
    catalog mapping (quote-based targets have no self-serve buy link); (b) the
    catalog entry exists but has ``payment_link_url=None`` (misconfig — logged
    at WARNING so the operator sees it in the ledger); (c) the mapped sku is
    absent from the catalog entirely (also WARNING).

    Read-through, no caching — the catalog is a ~15-entry Python list and this
    runs once per upsell send. Every call sees whatever the current catalog says.
    """
    sku_id = _TARGET_SKU_MAP.get(target_offer_code, "")
    if not sku_id:
        return ""
    # Local import to avoid a module-load-time cycle with backend.catalog,
    # which may transitively import finance modules.
    from backend.catalog.registry import CATALOG

    for entry in CATALOG:
        if entry.sku_id == sku_id:
            url = (entry.payment_link_url or "").strip()
            if not url:
                _LOG.warning(
                    "upsell target %s -> sku %s has no payment_link_url in "
                    "catalog; upsell email will render without a buy link",
                    target_offer_code, sku_id,
                )
            return url
    _LOG.warning(
        "upsell target %s -> sku %s not found in catalog; upsell email will "
        "render without a buy link",
        target_offer_code, sku_id,
    )
    return ""


def _public_page(target_offer_code: str) -> str:
    return _PUBLIC_PAGE_URL_BY_TARGET.get(target_offer_code, "")


def _payment_link(
    target_offer_code: str,
    queue_event_id: str | None = None,
    promotion_code: str | None = None,
) -> str:
    """Return the buy link with optional Stripe URL params.

    When ``queue_event_id`` is non-empty, ``client_reference_id=upsell_<id>``
    is appended — Stripe propagates this to
    ``checkout.session.completed.client_reference_id`` so the webhook
    handler can attribute the conversion via ``upsell_queue.mark_converted()``.

    When ``promotion_code`` is non-empty, ``prefilled_promotion_code=<code>``
    is appended — Stripe Payment Links pre-applies the discount at checkout
    so the customer sees the credit-applied price (e.g. $51 instead of $200)
    without manual entry. The coupon is one-time and tied to a single redemption.

    32-char hex event_id + short promo code keeps the URL well under Stripe's
    200-char limit on ``client_reference_id``.
    """
    base = _catalog_payment_link(target_offer_code)
    if not base:
        return base
    if queue_event_id:
        base = _append_query_param(base, "client_reference_id", f"upsell_{queue_event_id}")
    if promotion_code:
        base = _append_query_param(base, "prefilled_promotion_code", promotion_code)
    return base


def _target_name(target_offer_code: str) -> str:
    return _TARGET_DISPLAY_NAME.get(target_offer_code, target_offer_code or "the next step")


def _monthly_price(target_offer_code: str) -> int:
    return _TARGET_MONTHLY_USD.get(target_offer_code, 0)


def _one_time_price(target_offer_code: str) -> int:
    return _TARGET_ONE_TIME_USD.get(target_offer_code, 0)


def _quote_range(target_offer_code: str) -> tuple[int, int]:
    """Return (low, high) USD quote range for human-in-the-loop targets."""
    return _TARGET_QUOTE_RANGE_USD.get(target_offer_code, (0, 0))


# ---------------------------------------------------------------------------
# Per-touch composers (seo_audit → seo_implementation)
# The audit identifies issues; SEO Implementation is the $200 one-time fix
# pass that applies the prioritized list. Not the monthly retainer — that's
# what _seo_impl_touch_* upsells to next.
# ---------------------------------------------------------------------------


def _seo_audit_touch_1(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+5 — five days after the audit landed.

    Frame: 'you've read the audit, here's the fix — and your $149 audit
    purchase credits toward it, so you only pay the $51 difference.' The
    credit framing is load-bearing — it converts the upsell from a new
    purchase into 'finishing what you started.'
    """
    name = _target_name(target_offer_code)
    price = _one_time_price(target_offer_code)
    credit = 149   # SEO Audit price applies as credit toward Implementation
    net = price - credit
    page = _public_page(target_offer_code)
    link = _payment_link(target_offer_code, queue_event_id)

    subject = "Following up on your SEO audit — fixes for $51 net"

    text_body = (
        "Hi,\n"
        "\n"
        "Five days since your SEO audit landed. The report has the prioritized "
        "fix list — the question now is who's actually going to ship them.\n"
        "\n"
        f"That's what {name} is. Sticker price is ${price}, but the ${credit} "
        f"you paid for the audit applies as credit toward it — so you only "
        f"owe ${net}. We take your audit's P0/P1 fix list and apply it "
        "directly: title and meta updates, broken links repaired, schema "
        "markup, internal linking corrections, the Core-Web-Vitals quick "
        "wins. Typically wrapped in a week, with a before/after change log "
        "you can keep on file.\n"
        "\n"
        f"Overview:  {page}\n"
        f"Buy:       {link}\n"
        "\n"
        "If you're already handing this off internally, no hard feelings — "
        "just wanted to make sure the option was on the table before the "
        "audit findings go stale.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>Five days since your SEO audit landed. The report has the "
        "prioritized fix list — the question now is who's actually going "
        "to ship them.</p>"
        f"<p>That's what <strong>{name}</strong> is. Sticker price is "
        f"<strong>${price}</strong>, but the <strong>${credit}</strong> you "
        f"paid for the audit applies as credit toward it — so you only owe "
        f"<strong>${net}</strong>. We take your audit's P0/P1 fix list and "
        "apply it directly: title and meta updates, broken links repaired, "
        "schema markup, internal linking corrections, the Core-Web-Vitals "
        "quick wins. Typically wrapped in a week, with a before/after change "
        "log you can keep on file.</p>"
        "<ul>"
        f"<li>Overview: <a href=\"{page}\">{page}</a></li>"
        f"<li>Buy: <a href=\"{link}\">{link}</a></li>"
        "</ul>"
        "<p>If you're already handing this off internally, no hard feelings "
        "— just wanted to make sure the option was on the table before the "
        "audit findings go stale.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _seo_audit_touch_2(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+12 — twelve days after the audit. Lead with the math."""
    name = _target_name(target_offer_code)
    price = _one_time_price(target_offer_code)
    credit = 149
    net = price - credit
    page = _public_page(target_offer_code)
    link = _payment_link(target_offer_code, queue_event_id)

    subject = f"${net} to finish what your audit started"

    text_body = (
        "Hi again,\n"
        "\n"
        "Twelve days since your audit. The pattern we see most often: the "
        "audit identifies real issues, the operator agrees they're real, and "
        "then weeks pass without any of them getting fixed. Not because the "
        "operator doesn't care — just because the fixes never make it to "
        "the top of the week.\n"
        "\n"
        f"{name} exists for exactly that gap. The full price is ${price}, "
        f"but the ${credit} you already paid for the audit applies as credit "
        f"— so you owe ${net} to have us apply the P0/P1 fixes directly to "
        "your site (or deliver them as a versioned change set with "
        "apply-instructions if we don't have direct site access). One week "
        "from purchase to a before/after change log in your inbox.\n"
        "\n"
        f"Overview:  {page}\n"
        f"Buy:       {link}\n"
        "\n"
        "Happy to answer scope questions before you buy — just hit reply.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi again,</p>"
        "<p>Twelve days since your audit. The pattern we see most often: "
        "the audit identifies real issues, the operator agrees they're real, "
        "and then weeks pass without any of them getting fixed. Not because "
        "the operator doesn't care — just because the fixes never make it "
        "to the top of the week.</p>"
        f"<p><strong>{name}</strong> exists for exactly that gap. The full "
        f"price is <strong>${price}</strong>, but the <strong>${credit}</strong> "
        f"you already paid for the audit applies as credit — so you owe "
        f"<strong>${net}</strong> to have us apply the P0/P1 fixes directly "
        "to your site (or deliver them as a versioned change set with "
        "apply-instructions if we don't have direct site access). One week "
        "from purchase to a before/after change log in your inbox.</p>"
        "<ul>"
        f"<li>Overview: <a href=\"{page}\">{page}</a></li>"
        f"<li>Buy: <a href=\"{link}\">{link}</a></li>"
        "</ul>"
        "<p>Happy to answer scope questions before you buy — just hit reply.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _seo_audit_touch_3(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+30 — last touch from this cadence."""
    name = _target_name(target_offer_code)
    price = _one_time_price(target_offer_code)
    credit = 149
    net = price - credit
    page = _public_page(target_offer_code)
    link = _payment_link(target_offer_code, queue_event_id)

    subject = "Last touch from me — door's still open"

    text_body = (
        "Hi,\n"
        "\n"
        "Last note from me — a month since your audit. Three options from "
        "where I sit:\n"
        "\n"
        "1. You (or someone on your team) shipped the audit fixes — great. "
        "That's a real outcome and the audit did its job. No further action "
        "needed.\n"
        "\n"
        f"2. The fixes haven't shipped yet and you want help — {name} is "
        f"for that. ${net} net (your ${credit} audit credit applies against "
        f"the ${price} sticker), we apply the prioritized fix list and send "
        "a before/after log:\n"
        f"   {page}\n"
        f"   Buy: {link}\n"
        "\n"
        "3. The audit told you something different than you expected and "
        "you'd rather talk it through first — just hit reply with a time "
        "that works and we'll find ten minutes.\n"
        "\n"
        "I won't email a fourth time. The audit-credit offer stays valid "
        "if you come back later — just reply and we'll re-issue the link.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>Last note from me — a month since your audit. Three options from "
        "where I sit:</p>"
        "<ol>"
        "<li><strong>You (or your team) shipped the audit fixes</strong> — "
        "great. That's a real outcome and the audit did its job. No further "
        "action needed.</li>"
        f"<li><strong>The fixes haven't shipped and you want help</strong> — "
        f"{name} is for that. <strong>${net} net</strong> (your ${credit} "
        f"audit credit applies against the ${price} sticker), we apply the "
        "prioritized fix list and send a before/after log:<br>"
        f"&nbsp;&nbsp;Overview: <a href=\"{page}\">{page}</a><br>"
        f"&nbsp;&nbsp;Buy: <a href=\"{link}\">{link}</a></li>"
        "<li><strong>The audit told you something different than you "
        "expected</strong> and you'd rather talk it through first — just "
        "hit reply with a time and we'll find ten minutes.</li>"
        "</ol>"
        "<p>I won't email a fourth time. The audit-credit offer stays valid "
        "if you come back later — just reply and we'll re-issue the link.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


# ---------------------------------------------------------------------------
# Per-touch composers (seo_implementation → seo_optimization)
# After the one-time fix has landed, pitch the monthly retainer. The "fixes
# need to compound" angle is the through-line.
# ---------------------------------------------------------------------------


def _seo_impl_touch_1(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+5 — five days after the implementation deliverable shipped.

    Frame: 'the fixes are in — keep the gains compounding, your $200
    Implementation purchase credits toward month 1.' First month = $100,
    then $300/mo recurring.
    """
    name = _target_name(target_offer_code)
    price = _monthly_price(target_offer_code)        # $300/mo
    credit = 200                                      # Implementation price
    first_month = price - credit                      # $100
    page = _public_page(target_offer_code)
    link = _payment_link(target_offer_code, queue_event_id)

    subject = "Fixes shipped — keep going monthly for $100 the first month"

    text_body = (
        "Hi,\n"
        "\n"
        "Five days since your SEO Implementation deliverable shipped. The "
        "P0/P1 fixes from your audit are in. Question is what happens next.\n"
        "\n"
        "SEO is a compounding game. The one-time fix pass solves the backlog "
        "you started with; what builds ranking from here is steady monthly "
        "work — new content cleanup, fresh internal links, schema for any "
        "new pages, watching rank movement and patching where it slips.\n"
        "\n"
        f"That's what {name} is. Regular price is ${price}/month — but the "
        f"${credit} you paid for Implementation credits toward your first "
        f"month, so month 1 is ${first_month}, then ${price}/month after. We "
        "own the execution every month: apply the next round of fixes, "
        "monitor rank + GSC traffic, send a visibility report at month-end.\n"
        "\n"
        f"Overview:  {page}\n"
        f"Start it:  {link}\n"
        "\n"
        "If you'd rather see how the implementation lands before committing "
        "to monthly, totally reasonable — give it 30-60 days and we can "
        "revisit. Just wanted to flag the credit while it's fresh.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>Five days since your SEO Implementation deliverable shipped. "
        "The P0/P1 fixes from your audit are in. Question is what happens "
        "next.</p>"
        "<p>SEO is a compounding game. The one-time fix pass solves the "
        "backlog you started with; what builds ranking from here is steady "
        "monthly work — new content cleanup, fresh internal links, schema "
        "for any new pages, watching rank movement and patching where it "
        "slips.</p>"
        f"<p>That's what <strong>{name}</strong> is. Regular price is "
        f"<strong>${price}/month</strong> — but the <strong>${credit}</strong> "
        f"you paid for Implementation credits toward your first month, so "
        f"<strong>month 1 is ${first_month}, then ${price}/month after</strong>. "
        "We own the execution every month: apply the next round of fixes, "
        "monitor rank + GSC traffic, send a visibility report at month-end.</p>"
        "<ul>"
        f"<li>Overview: <a href=\"{page}\">{page}</a></li>"
        f"<li>Start it: <a href=\"{link}\">{link}</a></li>"
        "</ul>"
        "<p>If you'd rather see how the implementation lands before "
        "committing to monthly, totally reasonable — give it 30-60 days and "
        "we can revisit. Just wanted to flag the credit while it's fresh.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _seo_impl_touch_2(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+12 — twelve days after implementation. Lean into the data angle."""
    name = _target_name(target_offer_code)
    price = _monthly_price(target_offer_code)
    credit = 200
    first_month = price - credit
    page = _public_page(target_offer_code)
    link = _payment_link(target_offer_code, queue_event_id)

    subject = "The first SEO data after a fix pass is the most useful"

    text_body = (
        "Hi again,\n"
        "\n"
        "Twelve days since the fix pass shipped. This is when the GSC data "
        "gets interesting — Google's recrawl picks up most of the changes "
        "in the 14-30-day window, and rank shifts start surfacing.\n"
        "\n"
        f"{name} is built around that signal loop. ${price}/month — every "
        "month we pull GSC, diff against the prior period, identify what "
        "moved, and apply the next round of fixes against pages that are "
        "now within striking distance of better positions. It's the "
        "difference between 'we did SEO once' and 'SEO compounds for us.'\n"
        "\n"
        f"Your ${credit} Implementation purchase credits toward month 1 — "
        f"so you'd pay ${first_month} this month, then ${price}/month going "
        "forward.\n"
        "\n"
        f"Overview:  {page}\n"
        f"Start it:  {link}\n"
        "\n"
        "Happy to look at your current GSC numbers and tell you what the "
        "first month's focus would likely be — just hit reply.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi again,</p>"
        "<p>Twelve days since the fix pass shipped. This is when the GSC "
        "data gets interesting — Google's recrawl picks up most of the "
        "changes in the 14-30-day window, and rank shifts start surfacing.</p>"
        f"<p><strong>{name}</strong> is built around that signal loop. "
        f"<strong>${price}/month</strong> — every month we pull GSC, diff "
        "against the prior period, identify what moved, and apply the next "
        "round of fixes against pages that are now within striking distance "
        "of better positions. It's the difference between 'we did SEO once' "
        "and 'SEO compounds for us.'</p>"
        f"<p>Your <strong>${credit}</strong> Implementation purchase credits "
        f"toward month 1 — so you'd pay <strong>${first_month} this month</strong>, "
        f"then <strong>${price}/month</strong> going forward.</p>"
        "<ul>"
        f"<li>Overview: <a href=\"{page}\">{page}</a></li>"
        f"<li>Start it: <a href=\"{link}\">{link}</a></li>"
        "</ul>"
        "<p>Happy to look at your current GSC numbers and tell you what the "
        "first month's focus would likely be — just hit reply.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _seo_impl_touch_3(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+30 — last touch from this cadence."""
    name = _target_name(target_offer_code)
    price = _monthly_price(target_offer_code)
    credit = 200
    first_month = price - credit
    page = _public_page(target_offer_code)
    link = _payment_link(target_offer_code, queue_event_id)

    subject = "Last note from me — 30 days post-fix"

    text_body = (
        "Hi,\n"
        "\n"
        "A month since the fix pass. By now GSC should be showing real "
        "movement (impressions up, average position improving on the "
        "patched pages). Two paths from here:\n"
        "\n"
        "1. The fixes did what you needed and you'll let it ride. Fair "
        "outcome — call us back when there's new content or a redesign and "
        "the fix list grows again.\n"
        "\n"
        f"2. The movement is real and you want to compound it monthly — "
        f"{name} is the monthly retainer for that. Your ${credit} "
        f"Implementation credit applies to month 1, so it's ${first_month} "
        f"the first month, then ${price}/month after:\n"
        f"   {page}\n"
        f"   Start it: {link}\n"
        "\n"
        "I won't email about this again. The Implementation credit stays "
        "valid if you come back later — just reply and we'll re-issue the link.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>A month since the fix pass. By now GSC should be showing real "
        "movement (impressions up, average position improving on the "
        "patched pages). Two paths from here:</p>"
        "<ol>"
        "<li><strong>The fixes did what you needed and you'll let it ride.</strong> "
        "Fair outcome — call us back when there's new content or a redesign "
        "and the fix list grows again.</li>"
        f"<li><strong>The movement is real and you want to compound it monthly</strong> "
        f"— {name} is the monthly retainer for that. Your <strong>${credit}</strong> "
        f"Implementation credit applies to month 1, so it's <strong>${first_month} "
        f"the first month, then ${price}/month after</strong>:<br>"
        f"&nbsp;&nbsp;Overview: <a href=\"{page}\">{page}</a><br>"
        f"&nbsp;&nbsp;Start it: <a href=\"{link}\">{link}</a></li>"
        "</ol>"
        "<p>I won't email about this again. The Implementation credit stays "
        "valid if you come back later — just reply and we'll re-issue the link.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


# ---------------------------------------------------------------------------
# Per-touch composers (service_workflow_rescue → service_workflow_buildout)
# Quote-based hop: Buildout is human-in-the-loop priced ($2,500-$3,000), so
# the CTA is "reply to scope" rather than a static buy link. Operator
# generates a custom Stripe invoice per customer with the $500 rescue
# credit applied as a line-item discount.
# ---------------------------------------------------------------------------


def _rescue_to_buildout_touch_1(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+5 — five days after the 48h Workflow Rescue handoff."""
    name = _target_name(target_offer_code)
    low, high = _quote_range(target_offer_code)
    credit = 500
    net_low, net_high = low - credit, high - credit
    page = _public_page(target_offer_code)

    subject = "Want us to turn that rescue into a full system?"

    text_body = (
        "Hi,\n"
        "\n"
        "Five days since your Workflow Rescue shipped. The one workflow we "
        "fixed is running — but the pattern we see is that once one bottleneck "
        "is removed, the next one upstream becomes the new constraint. The "
        "broken hand-offs between your other systems usually surface within a "
        "month.\n"
        "\n"
        f"That's where {name} comes in. We connect the fragmented systems into "
        "a single execution layer over 14 days: discovery → build → integrate "
        "→ validate → handoff. Scope is conversational because every system "
        f"map is different — typical range is ${low:,}-${high:,}, and your "
        f"${credit} Workflow Rescue applies as credit (so you'd be looking at "
        f"${net_low:,}-${net_high:,} net depending on scope).\n"
        "\n"
        f"Overview:  {page}\n"
        "\n"
        "If this is interesting, hit reply with a paragraph or two about your "
        "current system landscape (what tools, where the hand-off pain is) "
        "and I'll send back a scoped quote within one business day.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>Five days since your Workflow Rescue shipped. The one workflow we "
        "fixed is running — but the pattern we see is that once one bottleneck "
        "is removed, the next one upstream becomes the new constraint. The "
        "broken hand-offs between your other systems usually surface within a "
        "month.</p>"
        f"<p>That's where <strong>{name}</strong> comes in. We connect the "
        "fragmented systems into a single execution layer over 14 days: "
        "discovery → build → integrate → validate → handoff. Scope is "
        f"conversational — typical range is <strong>${low:,}-${high:,}</strong>, "
        f"and your <strong>${credit}</strong> Workflow Rescue applies as "
        f"credit (so you'd be looking at <strong>${net_low:,}-${net_high:,} "
        f"net</strong> depending on scope).</p>"
        f"<p>Overview: <a href=\"{page}\">{page}</a></p>"
        "<p>If this is interesting, hit reply with a paragraph or two about "
        "your current system landscape (what tools, where the hand-off pain "
        "is) and I'll send back a scoped quote within one business day.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _rescue_to_buildout_touch_2(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+12 — twelve days post-rescue. Lead with the integration angle."""
    name = _target_name(target_offer_code)
    low, high = _quote_range(target_offer_code)
    credit = 500
    page = _public_page(target_offer_code)

    subject = "Two weeks in — where the next leverage usually shows up"

    text_body = (
        "Hi again,\n"
        "\n"
        "Twelve days since the rescue. You've probably already noticed the "
        "next bottleneck — something downstream that you used to work around "
        "because the first one was already broken. That's where the leverage "
        "lives now.\n"
        "\n"
        f"{name} is the engagement built for that. Two weeks, end-to-end: we "
        "map every system you touch, build the missing connections, validate "
        "with live data, hand off with a runbook per workflow. Typical scope "
        f"lands ${low:,}-${high:,}, and the ${credit} you paid for the rescue "
        "credits against the final invoice.\n"
        "\n"
        f"Overview:  {page}\n"
        "\n"
        "If you want to scope this out, reply with: (1) the one or two "
        "biggest hand-off pain points you're feeling, and (2) which tools "
        "are involved. I'll send back a quote within a business day.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi again,</p>"
        "<p>Twelve days since the rescue. You've probably already noticed the "
        "next bottleneck — something downstream that you used to work around "
        "because the first one was already broken. That's where the leverage "
        "lives now.</p>"
        f"<p><strong>{name}</strong> is the engagement built for that. Two "
        "weeks, end-to-end: we map every system you touch, build the missing "
        "connections, validate with live data, hand off with a runbook per "
        f"workflow. Typical scope lands <strong>${low:,}-${high:,}</strong>, "
        f"and the <strong>${credit}</strong> you paid for the rescue credits "
        "against the final invoice.</p>"
        f"<p>Overview: <a href=\"{page}\">{page}</a></p>"
        "<p>If you want to scope this out, reply with: (1) the one or two "
        "biggest hand-off pain points you're feeling, and (2) which tools "
        "are involved. I'll send back a quote within a business day.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _rescue_to_buildout_touch_3(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+30 — last touch from this cadence."""
    name = _target_name(target_offer_code)
    low, high = _quote_range(target_offer_code)
    credit = 500
    page = _public_page(target_offer_code)

    subject = "Last note — your $500 still counts toward a Buildout"

    text_body = (
        "Hi,\n"
        "\n"
        "A month since the rescue. Last note from me on this. Two paths "
        "where I sit:\n"
        "\n"
        "1. The one workflow we built handled the pain and you're good. "
        "Real outcome — that's what the rescue is for. Call us back when "
        "the next thing breaks and we'll do another.\n"
        "\n"
        f"2. You're feeling the next constraint and want to fix more of the "
        f"system. That's {name}. Typical scope ${low:,}-${high:,}, and your "
        f"${credit} from the rescue still counts as credit toward the quote.\n"
        "\n"
        f"   Overview: {page}\n"
        "\n"
        "I won't email about this thread again. The rescue credit stays "
        "valid — if you pick this back up in 3 months, just reply and we'll "
        "scope from there.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>A month since the rescue. Last note from me on this. Two paths "
        "where I sit:</p>"
        "<ol>"
        "<li><strong>The one workflow we built handled the pain and you're "
        "good.</strong> Real outcome — that's what the rescue is for. Call "
        "us back when the next thing breaks and we'll do another.</li>"
        f"<li><strong>You're feeling the next constraint and want to fix "
        f"more of the system.</strong> That's {name}. Typical scope "
        f"<strong>${low:,}-${high:,}</strong>, and your <strong>${credit}"
        f"</strong> from the rescue still counts as credit toward the quote.<br>"
        f"&nbsp;&nbsp;Overview: <a href=\"{page}\">{page}</a></li>"
        "</ol>"
        "<p>I won't email about this thread again. The rescue credit stays "
        "valid — if you pick this back up in 3 months, just reply and we'll "
        "scope from there.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


# ---------------------------------------------------------------------------
# Per-touch composers (service_workflow_buildout → service_ai_ops_partner_build)
# Quote-based hop: AI Ops Build is human-in-the-loop priced ($2,000-$5,000),
# scoped per engagement. CTA is "reply to scope". Buildout credit applies to
# the build invoice, not the retainer.
# ---------------------------------------------------------------------------


def _buildout_to_aiops_touch_1(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+5 — five days after the Buildout handoff."""
    name = _target_name(target_offer_code)
    low, high = _quote_range(target_offer_code)
    credit = 2500
    page = _public_page(target_offer_code)

    subject = "The system's running — want us to keep building on it?"

    text_body = (
        "Hi,\n"
        "\n"
        "Five days since your Workflow System Buildout shipped. The integrated "
        "execution layer is live — same question every Buildout customer "
        "eventually asks: do you want to keep extending it, or take it from "
        "here yourself?\n"
        "\n"
        f"Two paths inside the AI Ops Partner program:\n"
        "\n"
        f"  • {name} (build + premium retainer) — 30-day build, 3-8 more "
        "workflows on top of what's there, monitoring + alerts wired, "
        "runbooks shipped, then $5,000/mo for ongoing upkeep. Build scope is "
        f"conversational (typical ${low:,}-${high:,}), and your ${credit:,} "
        "Buildout credit applies to the build invoice.\n"
        "\n"
        "  • AI Ops Partner entry ($2,000/mo, no separate build) — lighter "
        "ongoing engagement: onboarding audit + first-month roadmap, then "
        "monthly continuous-improvement cycle. Better fit if you don't need "
        "the heavy build, just the steady hand.\n"
        "\n"
        f"Overview:  {page}\n"
        "\n"
        "If either is interesting, reply with a paragraph about where you'd "
        "want this to go and I'll send back the right path + a scoped quote "
        "within one business day.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>Five days since your Workflow System Buildout shipped. The "
        "integrated execution layer is live — same question every Buildout "
        "customer eventually asks: do you want to keep extending it, or take "
        "it from here yourself?</p>"
        "<p>Two paths inside the AI Ops Partner program:</p>"
        "<ul>"
        f"<li><strong>{name}</strong> (build + premium retainer) — 30-day "
        "build, 3-8 more workflows, monitoring + alerts wired, runbooks "
        "shipped, then <strong>$5,000/mo</strong> for ongoing upkeep. Build "
        f"scope is conversational (typical <strong>${low:,}-${high:,}</strong>), "
        f"and your <strong>${credit:,}</strong> Buildout credit applies to "
        "the build invoice.</li>"
        "<li><strong>AI Ops Partner entry</strong> (<strong>$2,000/mo</strong>, "
        "no separate build) — lighter ongoing engagement: onboarding audit + "
        "first-month roadmap, then monthly continuous-improvement cycle. "
        "Better fit if you don't need the heavy build, just the steady hand.</li>"
        "</ul>"
        f"<p>Overview: <a href=\"{page}\">{page}</a></p>"
        "<p>If either is interesting, reply with a paragraph about where "
        "you'd want this to go and I'll send back the right path + a scoped "
        "quote within one business day.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _buildout_to_aiops_touch_2(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+12 — twelve days post-Buildout. Lead with the maintenance angle."""
    name = _target_name(target_offer_code)
    low, high = _quote_range(target_offer_code)
    credit = 2500
    page = _public_page(target_offer_code)

    subject = "Two weeks in — every system you don't watch eventually drifts"

    text_body = (
        "Hi again,\n"
        "\n"
        "Twelve days since the Buildout. By now you'll have noticed which "
        "automations are doing real work and which need a second pass. The "
        "general pattern: the systems you actively watch keep working, the "
        "ones you don't quietly drift until something downstream breaks.\n"
        "\n"
        f"{name} is what closes that gap. We extend the operations engine "
        "with the next set of automations (3-8 more typical), wire monitoring "
        "so drift surfaces before it becomes failure, and ship the runbooks + "
        f"handoff package. Scope ranges ${low:,}-${high:,}, and your "
        f"${credit:,} Buildout credit applies to the build invoice. The "
        "$5,000/mo retainer that follows owns the maintenance loop so you're "
        "not watching dashboards yourself.\n"
        "\n"
        f"Overview:  {page}\n"
        "\n"
        "If you want to scope this, reply with: (1) what's been working in the "
        "Buildout so far, and (2) the 2-3 next things you'd want automated. "
        "Quote back in one business day.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi again,</p>"
        "<p>Twelve days since the Buildout. By now you'll have noticed which "
        "automations are doing real work and which need a second pass. The "
        "general pattern: the systems you actively watch keep working, the "
        "ones you don't quietly drift until something downstream breaks.</p>"
        f"<p><strong>{name}</strong> is what closes that gap. We extend the "
        "operations engine with the next set of automations (3-8 more typical), "
        "wire monitoring so drift surfaces before it becomes failure, and ship "
        "the runbooks + handoff package. Scope ranges "
        f"<strong>${low:,}-${high:,}</strong>, and your "
        f"<strong>${credit:,}</strong> Buildout credit applies to the build "
        "invoice. The <strong>$5,000/mo</strong> retainer that follows owns "
        "the maintenance loop so you're not watching dashboards yourself.</p>"
        f"<p>Overview: <a href=\"{page}\">{page}</a></p>"
        "<p>If you want to scope this, reply with: (1) what's been working "
        "in the Buildout so far, and (2) the 2-3 next things you'd want "
        "automated. Quote back in one business day.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


def _buildout_to_aiops_touch_3(
    target_offer_code: str,
    queue_event_id: str | None = None,
) -> tuple[str, str, str]:
    """D+30 — last touch from this cadence."""
    name = _target_name(target_offer_code)
    low, high = _quote_range(target_offer_code)
    credit = 2500
    page = _public_page(target_offer_code)

    subject = "Last note — the ops engine path stays open"

    text_body = (
        "Hi,\n"
        "\n"
        "A month since the Buildout shipped. Last note from me. Four paths "
        "from here:\n"
        "\n"
        "1. The Buildout is doing what you needed and you'll run it yourself. "
        "Real outcome — call us back when there's a next layer to add and "
        "we'll re-scope.\n"
        "\n"
        "2. Lighter ongoing engagement — AI Ops Partner entry at $2,000/mo. "
        "No separate build; we do an onboarding audit + first-month roadmap, "
        "then monthly continuous-improvement cycles. Self-serve from the page.\n"
        "\n"
        f"3. You want the next layer built but not the long-term maintenance. "
        f"{name} alone (no retainer) is fine — typical scope ${low:,}-${high:,}, "
        f"your ${credit:,} Buildout credit applies. Build then we hand off.\n"
        "\n"
        "4. Full operations engine + ongoing upkeep. Same build as #3, plus "
        "the $5,000/mo premium retainer that follows it. The retainer is what "
        "most heavier engagements settle into so the operator isn't watching "
        "their own dashboards.\n"
        "\n"
        f"   Overview: {page}\n"
        "\n"
        "I won't email about this thread again. The Buildout credit stays "
        "valid (paths 3 + 4 only) — if you come back in 6 months, just reply "
        "and we'll scope from there.\n"
        "\n"
        "— Morgan, HustleForge\n"
    )

    html_body = (
        "<p>Hi,</p>"
        "<p>A month since the Buildout shipped. Last note from me. Four "
        "paths from here:</p>"
        "<ol>"
        "<li><strong>The Buildout is doing what you needed and you'll run "
        "it yourself.</strong> Real outcome — call us back when there's a "
        "next layer to add and we'll re-scope.</li>"
        "<li><strong>Lighter ongoing engagement</strong> — AI Ops Partner "
        "entry at <strong>$2,000/mo</strong>. No separate build; we do an "
        "onboarding audit + first-month roadmap, then monthly "
        "continuous-improvement cycles. Self-serve from the page.</li>"
        f"<li><strong>You want the next layer built but not the long-term "
        f"maintenance.</strong> {name} alone (no retainer) — typical scope "
        f"<strong>${low:,}-${high:,}</strong>, your <strong>${credit:,}</strong> "
        "Buildout credit applies. Build then we hand off.</li>"
        "<li><strong>Full operations engine + ongoing upkeep.</strong> Same "
        "build as #3, plus the <strong>$5,000/mo</strong> premium retainer "
        "that follows it. Most heavier engagements settle here so the "
        "operator isn't watching their own dashboards.<br>"
        f"&nbsp;&nbsp;Overview: <a href=\"{page}\">{page}</a></li>"
        "</ol>"
        "<p>I won't email about this thread again. The Buildout credit stays "
        "valid (paths 3 + 4 only) — if you come back in 6 months, just reply "
        "and we'll scope from there.</p>"
        "<p>— Morgan, HustleForge</p>"
    )
    return subject, text_body, html_body


# Per-source-offer-code touch index. Add new mappings as new source offers
# need upsell sequences. Each callable accepts (target_offer_code,
# queue_event_id) and returns (subject, text, html). queue_event_id is
# passed through to _payment_link() for Cut 3 attribution.
_COMPOSER_SIG = Callable[[str, str | None], tuple[str, str, str]]
_COMPOSER_BY_SOURCE: dict[str, dict[int, _COMPOSER_SIG]] = {
    "seo_audit": {
        1: _seo_audit_touch_1,
        2: _seo_audit_touch_2,
        3: _seo_audit_touch_3,
    },
    "seo_implementation": {
        1: _seo_impl_touch_1,
        2: _seo_impl_touch_2,
        3: _seo_impl_touch_3,
    },
    "service_workflow_rescue": {
        1: _rescue_to_buildout_touch_1,
        2: _rescue_to_buildout_touch_2,
        3: _rescue_to_buildout_touch_3,
    },
    "service_workflow_buildout": {
        1: _buildout_to_aiops_touch_1,
        2: _buildout_to_aiops_touch_2,
        3: _buildout_to_aiops_touch_3,
    },
}


def _compliance_footer() -> tuple[str, str]:
    """Return ``(text, html)`` CAN-SPAM footer appended to every upsell email.

    Upsell emails are commercial, so a physical postal address + a working
    opt-out are legally required (CAN-SPAM) — and are exactly what the
    ComplianceGuard checks for before a commercial send. Values come from
    settings (``sender_postal_address`` / ``unsubscribe_url``); a missing
    unsubscribe URL falls back to a reply-to-opt-out instruction (still a valid
    mechanism — inbound replies are opt-out-classified by the reply pods).
    """
    from backend.common.config import get_settings
    s = get_settings()
    postal = str(getattr(s, "sender_postal_address", "") or "").strip()
    unsub = str(getattr(s, "unsubscribe_url", "") or "").strip()

    text_lines = ["", "--"]
    if postal:
        text_lines.append(postal)
    text_lines.append(
        f"Unsubscribe: {unsub}" if unsub else "Reply STOP to unsubscribe at any time."
    )
    text = "\n".join(text_lines)

    html_parts = ['<hr><p style="font-size:12px;color:#888;">']
    if postal:
        html_parts.append(f"{postal}<br>")
    html_parts.append(
        f'Unsubscribe: <a href="{unsub}">{unsub}</a>' if unsub
        else "Reply STOP to unsubscribe at any time."
    )
    html_parts.append("</p>")
    return text, "".join(html_parts)


def render_upsell_email(
    *,
    source_offer_code: str,
    target_offer_code: str,
    touch_num: int,
    queue_event_id: str | None = None,
    promotion_code: str | None = None,
) -> tuple[str, str, str, str] | None:
    """Return ``(subject, text_body, html_body, payment_link_url)`` or None
    when no composer exists for this source-offer + touch_num.

    When ``queue_event_id`` is non-empty, the payment_link_url has
    ``?client_reference_id=upsell_<event_id>`` appended (Cut 3 attribution).

    When ``promotion_code`` is non-empty, the payment_link_url ALSO has
    ``?prefilled_promotion_code=<code>`` appended so the customer's Stripe
    checkout shows the credit-applied price without manual entry. The
    composer body still describes the credit math in human-readable form
    (e.g. "Sticker $200, your $149 audit credit applies, $51 net"); the
    URL parameter just makes the auto-apply happen at checkout.

    The runner translates a None return into a ``failed`` row with
    ``error='no_composer'`` rather than sending a malformed email.
    """
    composers = _COMPOSER_BY_SOURCE.get(source_offer_code)
    if composers is None:
        _LOG.warning(
            "render_upsell_email: no composer set for source_offer_code=%s",
            source_offer_code,
        )
        return None
    composer = composers.get(touch_num)
    if composer is None:
        _LOG.warning(
            "render_upsell_email: no composer for source=%s touch=%d",
            source_offer_code, touch_num,
        )
        return None

    # Composer functions still pass through queue_event_id only — the link
    # they bake into the body is identical to the one we return. We threaded
    # promotion_code into _payment_link() the same way; we re-render the link
    # here (with both params) so the returned payment_link includes the promo
    # too. Bodies are then post-processed via str.replace so the in-body link
    # matches what the customer would click.
    subject, text_body, html_body = composer(target_offer_code, queue_event_id)
    payment_link = _payment_link(
        target_offer_code,
        queue_event_id=queue_event_id,
        promotion_code=promotion_code,
    )
    if promotion_code:
        # Composer bodies embed the link without the promo param. Swap them
        # for the full link so a customer clicking from the email gets the
        # discount auto-applied.
        plain_link = _payment_link(target_offer_code, queue_event_id=queue_event_id)
        if plain_link and plain_link != payment_link:
            text_body = text_body.replace(plain_link, payment_link)
            html_body = html_body.replace(plain_link, payment_link)
    # Append the CAN-SPAM footer (postal address + unsubscribe). Mandatory for
    # these commercial emails and required to clear the ComplianceGuard before
    # a commercial send (cash-engine + campaign paths already carry it).
    text_footer, html_footer = _compliance_footer()
    text_body = text_body + text_footer
    html_body = html_body + html_footer
    return subject, text_body, html_body, payment_link


__all__ = ["render_upsell_email"]
