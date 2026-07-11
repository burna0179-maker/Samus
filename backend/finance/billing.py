"""Payment-link surface for the finance workcell (Phase 2, read-only).

The operator has pre-created live Stripe payment links for the canonical
product catalog and tagged each with ``metadata.hf_offer_code`` so Samus can
index them. This module surfaces those links via the workcell so the
morning briefing puts every "yes" within copy-paste range.

No write path yet — creating new links requires a Stripe key with
payment_link write scope; the current restricted-read key cannot. Adding
the write side is a separate decision (and a new Stripe key).
"""

from __future__ import annotations

import logging

from backend.common.config import get_settings

from .models import (
    PaymentLinkSummary,
    PaymentLinksRollup,
    StripePaymentLink,
)
from .stripe_client import StripeClient, StripeError


_LOG = logging.getLogger("samus.finance.billing")


def _summarize_one(link: StripePaymentLink) -> PaymentLinkSummary:
    md = link.metadata or {}
    return PaymentLinkSummary(
        id=link.id,
        offer_code=md.get("hf_offer_code", ""),
        url=link.url,
        is_subscription=link.is_subscription,
        livemode=link.livemode,
        samus_managed=(md.get("samus_managed", "").lower() == "true"),
    )


def summarize_links(links: list[StripePaymentLink]) -> list[PaymentLinkSummary]:
    """Convert Stripe payment_link rows to operator-facing summaries.

    Sort: livemode first, then samus_managed, then by offer_code alphabetical
    (offer_code-less entries sort last by id). This keeps "real" links
    above test ones and groups by name in the briefing output.
    """
    summaries = [_summarize_one(l) for l in links]

    def _key(s: PaymentLinkSummary) -> tuple[int, int, str]:
        return (
            0 if s.livemode else 1,
            0 if s.samus_managed else 1,
            s.offer_code or f"~~{s.id}",  # offer-code-less rows sort last
        )

    return sorted(summaries, key=_key)


def fetch_rollup(ts: str, *, active: bool = True) -> PaymentLinksRollup:
    """Pull live payment links from Stripe and roll up into the summary view.

    Stripe-side failures are caught — the rollup returns with
    ``stripe_reachable=False`` and the error message so the morning briefing
    can degrade gracefully instead of crashing.
    """
    settings = get_settings()
    key = (settings.stripe_api_key or "").strip()
    if not key:
        return PaymentLinksRollup(
            links=[],
            count_total=0,
            count_subscription=0,
            count_one_time=0,
            livemode_count=0,
            stripe_reachable=False,
            stripe_error="stripe_api_key_unset",
            ts=ts,
        )

    client = StripeClient(api_key=key)
    try:
        raw = client.fetch_payment_links(active=active, limit=100)
    except StripeError as exc:
        _LOG.warning("payment_links fetch failed: %s", exc)
        return PaymentLinksRollup(
            links=[],
            count_total=0,
            count_subscription=0,
            count_one_time=0,
            livemode_count=0,
            stripe_reachable=False,
            stripe_error=str(exc),
            ts=ts,
        )

    summaries = summarize_links(raw)
    return PaymentLinksRollup(
        links=summaries,
        count_total=len(summaries),
        count_subscription=sum(1 for s in summaries if s.is_subscription),
        count_one_time=sum(1 for s in summaries if not s.is_subscription),
        livemode_count=sum(1 for s in summaries if s.livemode),
        stripe_reachable=True,
        stripe_error=None,
        ts=ts,
    )
