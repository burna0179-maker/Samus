"""Audit-to-monthly-upsell touch queue (JSONL, append-only).

After ``fulfill.py`` advances a customer to ``delivered``, three upsell touches
are enqueued (D+5, D+12, D+30 from delivery). A background worker thread in
the finance workcell wakes hourly to send any rows whose ``due_at`` has
passed (see ``upsell_runner.process_upsell_queue``).

State machine per (customer_id, source_offer_code, touch_num):

    queued  --(runner sends)-->  sent      --(cut 2)-->  converted
       \\                            \\                       \\
        '-(dup enqueue)-> skipped_dup '-(adapter raises)-> failed

Each transition appends a NEW row referencing the original ``event_id``.
Current state for a key = latest row's ``kind`` field. This mirrors the
append-only stripe_events.jsonl pattern in webhook.py — same idempotency
guarantees, same fold-the-log read pattern.

Path defaults to ``/opt/samus/data/finance/upsell_queue.jsonl`` (env
override ``SAMUS_UPSELL_QUEUE_PATH``). Read/write is via
``backend.common.persistence.JsonlLedger``.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict

from backend.common import persistence


_LOG = logging.getLogger("samus.finance.upsell_queue")

_QUEUE_PATH_DEFAULT = "/opt/samus/data/finance/upsell_queue.jsonl"

# Standard nurture cadence — D+5, D+12, D+30 from delivered_at.
# Operator-tunable later via env if we want to test other cadences. For now
# the cadence is intentionally baked in so all rows in the ledger share the
# same schema.
DEFAULT_TOUCH_DELAYS_DAYS: tuple[int, ...] = (5, 12, 30)


UpsellRowKind = Literal[
    "queued",
    "sent",
    "failed",
    "converted",
    "skipped_dup",
]


class UpsellQueueRow(BaseModel):
    """One row in upsell_queue.jsonl. Represents a state transition."""
    model_config = ConfigDict(extra="forbid")

    event_id: str               # unique per row (uuid4 hex)
    ts: str                     # ISO UTC of this transition
    kind: UpsellRowKind
    touch_num: int              # 1, 2, or 3 — the position in the sequence
    customer_id: str
    customer_email: str
    source_offer_code: str      # e.g. "seo_audit"
    target_offer_code: str = ""     # e.g. "seo_implementation"
    target_price_id: str = ""       # e.g. "price_1TKu0a..."
    due_at: str = ""                # ISO UTC; only set on 'queued'
    sent_message_id: str = ""       # only set on 'sent'
    subscription_id: str = ""       # only set on 'converted'
    error: str = ""                 # only set on 'failed'
    # --- credit-application fields (Stripe coupon per customer×source) ----
    # Shared across all 3 touches of one customer's enqueue batch. Empty when
    # Stripe coupon creation failed or was skipped (no API key, dev env);
    # render_upsell_email then sends the email without an auto-applied
    # discount and the operator can manually attach a coupon if needed.
    coupon_id: str = ""             # Stripe coupon id, e.g. "iVklcKWE"
    promotion_code_id: str = ""     # Stripe promotion_code id, "promo_..."
    promotion_code: str = ""        # human-readable code, e.g. "AUDIT-CREDIT-7K2QXM"
    credit_usd_cents: int = 0       # amount_off the coupon carries


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def queue_path() -> Path:
    return Path(os.getenv("SAMUS_UPSELL_QUEUE_PATH", _QUEUE_PATH_DEFAULT))


def _ledger() -> persistence.Ledger:
    """Open the upsell queue via the pluggable backend (W-1).

    ``jsonl`` (default — local / VM) appends ``upsell_queue.jsonl``;
    ``firestore`` (Cloud Run) appends the ``upsell_queue`` collection so the
    append-only nurture-state log survives a scale-to-zero. See
    ``backend.common.persistence.open_ledger``.
    """
    return persistence.open_ledger(
        jsonl_path=str(queue_path()), collection="upsell_queue",
    )


def _append(row: UpsellQueueRow) -> None:
    try:
        _ledger().append(row.model_dump())
    except Exception as exc:  # noqa: BLE001 - best-effort; backend-agnostic
        _LOG.warning("upsell queue append failed: %s", exc)


def _read_all_rows() -> list[UpsellQueueRow]:
    out: list[UpsellQueueRow] = []
    for rec in _ledger().scan():
        try:
            out.append(UpsellQueueRow.model_validate(rec))
        except Exception:  # noqa: BLE001 — skip malformed
            continue
    return out


def _key(row: UpsellQueueRow) -> tuple[str, str, int]:
    """Group key per upsell touch: customer × source offer × touch number."""
    return (row.customer_id, row.source_offer_code, row.touch_num)


def _current_state_by_key(rows: list[UpsellQueueRow]) -> dict[tuple, UpsellQueueRow]:
    """Fold the append-only log → latest row per key."""
    latest: dict[tuple, UpsellQueueRow] = {}
    for r in rows:
        k = _key(r)
        prev = latest.get(k)
        if prev is None or r.ts >= prev.ts:
            latest[k] = r
    return latest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Per-source-offer-code mapping to the immediate next-step in the funnel.
# SEO funnel:        audit ($149) → implementation ($200) → optimization ($300/mo)
# Automation funnel: rescue ($500) → buildout ($2,500-3k) → ai_ops_partner_build ($2-5k)
#                    → retainer at $5k/mo follows the build engagement.
#
# ``quote_based=True`` marks hops where the target SKU is human-in-the-loop
# priced (Workflow Buildout, AI Ops Partner Build). For those hops:
#   * the upsell email asks the customer to reply (no static buy link)
#   * coupon creation is SKIPPED (operator applies credit on the custom
#     Stripe invoice they generate per-customer)
#   * ``target_price_id`` may be empty
UPSELL_TARGET_MAP: dict[str, dict[str, Any]] = {
    "seo_audit": {
        "target_offer_code": "seo_implementation",
        "target_price_id": "price_1TKuEeBdo4u4gpS7853xVNwz",
        "quote_based": False,
    },
    "seo_implementation": {
        "target_offer_code": "seo_optimization",
        "target_price_id": "price_1TKu0aBdo4u4gpS7I6ZPKIy6",
        "quote_based": False,
    },
    "service_workflow_rescue": {
        "target_offer_code": "service_workflow_buildout",
        # Buildout has a static Stripe price ($2,500 floor) but most quotes
        # land $2,500-$3,000 — operator generates a custom invoice for any
        # quote above the floor. We still record the floor price id so the
        # webhook can match self-serve purchases at the floor.
        "target_price_id": "price_1TAj1pBdo4u4gpS7JBbZ5SWJ",
        "quote_based": True,
    },
    "service_workflow_buildout": {
        "target_offer_code": "service_ai_ops_partner_build",
        # No static Stripe product for AI Ops Build — every customer is
        # invoiced manually after the scoping conversation.
        "target_price_id": "",
        "quote_based": True,
    },
}


# Per-source credit amount (in cents) applied toward the target SKU.
# For non-quote-based hops this is the Stripe coupon ``amount_off`` (created
# at enqueue time, attached to the buy link via prefilled_promotion_code).
# For quote-based hops it documents the credit the operator should apply when
# generating the custom invoice — no coupon is created.
# Stripe coupons cap at invoice amount, so excess credit is forfeit unless
# we switch the coupon to duration="repeating".
UPSELL_CREDIT_CENTS_BY_SOURCE: dict[str, int] = {
    "seo_audit": 14900,                  # $149 → toward $200 Implementation = $51 net
    "seo_implementation": 20000,         # $200 → toward $300/mo Optimization = $100 first month
    "service_workflow_rescue": 50000,    # $500 → toward $2,500-$3,000 Buildout = $2k-$2.5k net
    "service_workflow_buildout": 250000, # $2,500 → toward $2k-$5k AI Ops Build (varies, operator-applied)
}


# Bundle of credit-bearing fields propagated across all 3 touches of one
# (customer, source, target) batch.
class _CouponBundle(BaseModel):
    coupon_id: str = ""
    promotion_code_id: str = ""
    promotion_code: str = ""
    credit_usd_cents: int = 0


def _existing_coupon_for(
    rows: list[UpsellQueueRow],
    customer_id: str,
    source_offer_code: str,
) -> _CouponBundle | None:
    """Return the prior coupon bundle for (customer_id, source), if any.

    Scans the append-only log for any row from this customer×source whose
    coupon_id is non-empty. The first 3-touch enqueue creates the coupon
    once; a subsequent ``skipped_dup`` re-enqueue reuses it rather than
    burning a second Stripe coupon for the same funnel hop.
    """
    for row in rows:
        if (row.customer_id == customer_id
                and row.source_offer_code == source_offer_code
                and row.coupon_id):
            return _CouponBundle(
                coupon_id=row.coupon_id,
                promotion_code_id=row.promotion_code_id,
                promotion_code=row.promotion_code,
                credit_usd_cents=row.credit_usd_cents,
            )
    return None


def _generate_promo_code(source_offer_code: str) -> str:
    """Generate a Stripe-compatible promo code: <SOURCE_PREFIX>-CREDIT-<6 chars>.

    Stripe uppercases promotion codes server-side, so we generate uppercase
    + digits. 6 chars of entropy is enough — the coupon also has
    ``max_redemptions=1`` so even a guessed code can't be reused.
    """
    import secrets
    import string
    prefix_map = {
        "seo_audit": "AUDIT",
        "seo_implementation": "IMPL",
        "service_workflow_rescue": "RESCUE",
        "service_workflow_buildout": "BUILDOUT",
    }
    prefix = prefix_map.get(source_offer_code, source_offer_code.upper().replace("_", ""))
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"{prefix}-CREDIT-{suffix}"


def _default_create_coupon_fn(
    *,
    customer_id: str,
    customer_email: str,
    source_offer_code: str,
    target_offer_code: str,
    credit_cents: int,
) -> _CouponBundle:
    """Production coupon creator — calls Stripe.

    Returns an empty ``_CouponBundle`` (all-empty fields) on any failure or
    when ``STRIPE_API_KEY`` is unset. Callers persist the empty bundle to
    the queue row; the runner still sends the email (with credit mentioned
    in body text) but the buy link has no auto-applied discount. Operator
    can manually attach a coupon in the Stripe dashboard if needed.
    """
    try:
        from backend.common.config import get_settings
        from backend.finance.stripe_client import StripeClient, StripeError
    except ImportError as exc:
        _LOG.warning("coupon create skipped (import failed): %s", exc)
        return _CouponBundle(credit_usd_cents=credit_cents)

    settings = get_settings()
    api_key = getattr(settings, "stripe_api_key", "") or ""
    if not api_key:
        _LOG.info("coupon create skipped: stripe_api_key unset")
        return _CouponBundle(credit_usd_cents=credit_cents)

    client = StripeClient(api_key=api_key)
    name = f"{source_offer_code}→{target_offer_code} credit ({customer_email})"
    metadata = {
        "samus_customer_id": customer_id[:200],
        "samus_customer_email": customer_email[:200],
        "samus_source_offer_code": source_offer_code,
        "samus_target_offer_code": target_offer_code,
    }
    try:
        coupon = client.create_coupon(
            amount_off_cents=credit_cents,
            currency="usd",
            duration="once",
            max_redemptions=1,
            name=name[:160],   # Stripe name cap is 160 chars
            metadata=metadata,
        )
        coupon_id = str(coupon.get("id") or "")
        if not coupon_id:
            _LOG.warning("coupon create returned no id; raw=%s", coupon)
            return _CouponBundle(credit_usd_cents=credit_cents)

        promo_code = _generate_promo_code(source_offer_code)
        promo = client.create_promotion_code(
            coupon_id=coupon_id,
            code=promo_code,
            max_redemptions=1,
            metadata=metadata,
        )
        promo_id = str(promo.get("id") or "")
        # Stripe normalizes the code (uppercase); read it back to make sure
        # the email body uses what Stripe actually stored.
        final_code = str(promo.get("code") or promo_code)
        return _CouponBundle(
            coupon_id=coupon_id,
            promotion_code_id=promo_id,
            promotion_code=final_code,
            credit_usd_cents=credit_cents,
        )
    except StripeError as exc:
        _LOG.warning(
            "Stripe coupon create failed for %s %s→%s: %s",
            customer_email, source_offer_code, target_offer_code, exc,
        )
        return _CouponBundle(credit_usd_cents=credit_cents)
    except Exception as exc:  # noqa: BLE001 — best-effort; never raise from enqueue
        _LOG.warning(
            "Unexpected coupon create error for %s %s→%s: %s",
            customer_email, source_offer_code, target_offer_code, exc,
        )
        return _CouponBundle(credit_usd_cents=credit_cents)


def enqueue_upsell(
    *,
    customer_id: str,
    customer_email: str,
    source_offer_code: str,
    delivered_at: datetime | None = None,
    touch_delays_days: tuple[int, ...] = DEFAULT_TOUCH_DELAYS_DAYS,
    create_coupon_fn: "Callable[..., _CouponBundle] | None" = None,
) -> list[str]:
    """Queue the upsell sequence for one delivered customer.

    Returns the list of event_ids written (one per touch). For each touch,
    if a prior row already exists for (customer_id, source_offer_code,
    touch_num) — regardless of its current state — a ``skipped_dup`` row is
    appended instead of a fresh ``queued`` row, preserving the append-only
    log + preventing double-sends if fulfill.py runs twice for the same
    customer.

    Returns empty list if ``source_offer_code`` isn't mapped to an upsell
    target (no row written).

    Coupon creation: one Stripe coupon (with a wrapped promotion_code) is
    created per (customer_id, source_offer_code) batch and shared across
    all 3 touches. A re-enqueue (``skipped_dup`` path) reuses the prior
    coupon rather than burning a second one. ``create_coupon_fn`` is the
    test injection point; production omits it and uses Stripe.
    """
    target = UPSELL_TARGET_MAP.get(source_offer_code)
    if target is None:
        _LOG.info(
            "upsell skipped: no target mapped for source_offer_code=%s",
            source_offer_code,
        )
        return []

    delivered_at = delivered_at or datetime.now(timezone.utc)
    if delivered_at.tzinfo is None:
        delivered_at = delivered_at.replace(tzinfo=timezone.utc)

    all_rows = _read_all_rows()
    existing_keys = set(_current_state_by_key(all_rows).keys())

    # Resolve (or create) the coupon bundle once per batch.
    # Quote-based hops skip coupon creation entirely — the operator generates
    # a custom Stripe invoice per customer and applies the credit there. The
    # credit amount is still recorded on the queue row so the composer can
    # describe it in the email body and the operator has a reference value.
    bundle = _existing_coupon_for(all_rows, customer_id, source_offer_code)
    if bundle is None:
        credit_cents = UPSELL_CREDIT_CENTS_BY_SOURCE.get(source_offer_code, 0)
        is_quote_based = bool(target.get("quote_based"))
        if credit_cents > 0 and not is_quote_based:
            coupon_fn = create_coupon_fn or _default_create_coupon_fn
            bundle = coupon_fn(
                customer_id=customer_id,
                customer_email=customer_email,
                source_offer_code=source_offer_code,
                target_offer_code=target["target_offer_code"],
                credit_cents=credit_cents,
            )
        else:
            # Quote-based: empty Stripe fields, but record the credit amount
            # so the email body + queue row both surface it.
            bundle = _CouponBundle(credit_usd_cents=credit_cents)

    written: list[str] = []
    for touch_num, delay_days in enumerate(touch_delays_days, start=1):
        key = (customer_id, source_offer_code, touch_num)
        event_id = uuid.uuid4().hex
        if key in existing_keys:
            row = UpsellQueueRow(
                event_id=event_id,
                ts=_iso_now(),
                kind="skipped_dup",
                touch_num=touch_num,
                customer_id=customer_id,
                customer_email=customer_email,
                source_offer_code=source_offer_code,
                target_offer_code=target["target_offer_code"],
                target_price_id=target["target_price_id"],
                coupon_id=bundle.coupon_id,
                promotion_code_id=bundle.promotion_code_id,
                promotion_code=bundle.promotion_code,
                credit_usd_cents=bundle.credit_usd_cents,
            )
        else:
            due = delivered_at + timedelta(days=delay_days)
            row = UpsellQueueRow(
                event_id=event_id,
                ts=_iso_now(),
                kind="queued",
                touch_num=touch_num,
                customer_id=customer_id,
                customer_email=customer_email,
                source_offer_code=source_offer_code,
                target_offer_code=target["target_offer_code"],
                target_price_id=target["target_price_id"],
                due_at=due.strftime("%Y-%m-%dT%H:%M:%SZ"),
                coupon_id=bundle.coupon_id,
                promotion_code_id=bundle.promotion_code_id,
                promotion_code=bundle.promotion_code,
                credit_usd_cents=bundle.credit_usd_cents,
            )
        _append(row)
        written.append(event_id)
    return written


def due_upsells(now: datetime | None = None) -> list[UpsellQueueRow]:
    """Return rows whose current state is ``queued`` AND ``due_at <= now``.

    The runner consumes this list, sends an email per row, and appends a
    transition row (``sent`` / ``failed``) for each.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = _read_all_rows()
    latest = _current_state_by_key(rows)
    out: list[UpsellQueueRow] = []
    for row in latest.values():
        if row.kind != "queued":
            continue
        due = _parse_iso(row.due_at)
        if due is None:
            continue
        if due <= now:
            out.append(row)
    # Deterministic order: earliest due first, then by customer for stability.
    return sorted(out, key=lambda r: (r.due_at, r.customer_id, r.touch_num))


def mark_sent(*, queued_row: UpsellQueueRow, message_id: str) -> None:
    """Append a ``sent`` transition row referencing the queued row."""
    row = UpsellQueueRow(
        event_id=uuid.uuid4().hex,
        ts=_iso_now(),
        kind="sent",
        touch_num=queued_row.touch_num,
        customer_id=queued_row.customer_id,
        customer_email=queued_row.customer_email,
        source_offer_code=queued_row.source_offer_code,
        target_offer_code=queued_row.target_offer_code,
        target_price_id=queued_row.target_price_id,
        sent_message_id=message_id,
    )
    _append(row)


def mark_failed(*, queued_row: UpsellQueueRow, error: str) -> None:
    """Append a ``failed`` transition row. Error trimmed to 200 chars."""
    row = UpsellQueueRow(
        event_id=uuid.uuid4().hex,
        ts=_iso_now(),
        kind="failed",
        touch_num=queued_row.touch_num,
        customer_id=queued_row.customer_id,
        customer_email=queued_row.customer_email,
        source_offer_code=queued_row.source_offer_code,
        target_offer_code=queued_row.target_offer_code,
        target_price_id=queued_row.target_price_id,
        error=(error or "")[:200],
    )
    _append(row)


def mark_converted(
    *,
    customer_id: str,
    source_offer_code: str,
    touch_num: int,
    subscription_id: str,
) -> None:
    """Append a ``converted`` row when the subscription webhook lands.

    Used by Cut 2 (subscription.created webhook handler). The webhook
    handler decides which touch_num to credit (typically the most recent
    'sent' before the subscription created timestamp).
    """
    rows = _read_all_rows()
    # Find the matching 'sent' row to copy customer_email + target fields.
    match: UpsellQueueRow | None = None
    for r in rows:
        if (r.customer_id == customer_id
                and r.source_offer_code == source_offer_code
                and r.touch_num == touch_num
                and r.kind == "sent"):
            match = r
            break
    if match is None:
        _LOG.warning(
            "mark_converted: no prior 'sent' row found for %s/%s/touch%d",
            customer_id, source_offer_code, touch_num,
        )
        return
    row = UpsellQueueRow(
        event_id=uuid.uuid4().hex,
        ts=_iso_now(),
        kind="converted",
        touch_num=touch_num,
        customer_id=customer_id,
        customer_email=match.customer_email,
        source_offer_code=source_offer_code,
        target_offer_code=match.target_offer_code,
        target_price_id=match.target_price_id,
        subscription_id=subscription_id,
    )
    _append(row)


# ---------------------------------------------------------------------------
# Reads used by the briefing rollup
# ---------------------------------------------------------------------------


def load_recent_rows(window_days: int = 14) -> list[UpsellQueueRow]:
    """Return all rows with ``ts`` inside the trailing window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    out: list[UpsellQueueRow] = []
    for r in _read_all_rows():
        ts = _parse_iso(r.ts)
        if ts is None:
            continue
        if ts >= cutoff:
            out.append(r)
    return out


__all__ = [
    "UpsellQueueRow",
    "DEFAULT_TOUCH_DELAYS_DAYS",
    "UPSELL_TARGET_MAP",
    "queue_path",
    "enqueue_upsell",
    "due_upsells",
    "mark_sent",
    "mark_failed",
    "mark_converted",
    "load_recent_rows",
]
