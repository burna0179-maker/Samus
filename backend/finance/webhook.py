"""Stripe webhook handler — signature verify + customer auto-advance.

Phase 8 of the go-live plan. Stripe POSTs ``checkout.session.completed`` (and
related) events to a configured endpoint. We:

  1. Verify the ``Stripe-Signature`` header against ``STRIPE_WEBHOOK_SECRET``
     using HMAC-SHA256 + constant-time compare + replay-window tolerance.
  2. Parse the event into ``StripeWebhookEvent`` (subset; extra fields ignored).
  3. Idempotency-check on Stripe's ``event.id`` so retries don't double-process.
  4. For ``checkout.session.completed``:
       - Pull customer_email + amount + currency + hf_offer_code from the
         session payload + line_items metadata.
       - Find-or-create the customer in the memory workcell (Neo4j).
       - Advance state to ``paid``.
       - Append a WebhookEventRecord to the JSONL event log.
  5. For other event types: log as ``ignored`` (Stripe-side success so it
     stops retrying), no downstream side effects.

Auto-fulfillment (Win #2): when (a) the offer code is in the
``SAMUS_AUTO_FULFILL_OFFERS`` whitelist AND (b) the customer supplied a
website URL via a Stripe checkout custom_field (``key='website_url'``),
the handler spawns a daemon thread that runs ``fulfill_customer`` and
appends a second JSONL record (``event_type='auto_fulfill'``) with the
outcome. If either condition is missing the operator still runs
``Run-Fulfill.ps1`` manually after the morning briefing surfaces the
pending payment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from backend.common import persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.http_client import signed_post_json_sync

from .models import (
    StripeWebhookEvent,
    WebhookEventRecord,
    WebhookProcessResult,
)
from .receipts import send_payment_receipt


_LOG = logging.getLogger("samus.finance.webhook")

# Stripe's official replay window is 5 minutes. Operators can override via
# env-var when running through a slow tunnel (ngrok cold-start latency etc).
_DEFAULT_REPLAY_TOLERANCE_SECONDS = 300
# RT FIN-07: clock-skew allowance for FUTURE-dated Stripe timestamps.
_STRIPE_FUTURE_SKEW_SECONDS = 5

# JSONL event log path. Mirrors the audit-ledger pattern used elsewhere.
_DEFAULT_EVENT_LOG_PATH = "/opt/samus/data/finance/stripe_events.jsonl"

# RT FIN-01: state-advance fulfill gate. A checkout.session.completed only
# clears the customer to a fulfilled state when funds have actually settled.
# Stripe semantics: "paid" = charged; "no_payment_required" = $0 session
# (100%-coupon / free-trial). "unpaid" (ACH-pending / failed) must NOT trigger
# fulfillment — we record the event for visibility but withhold the advance
# until clearing (a future async_payment_succeeded handler would complete it).
_FULFILL_PAYMENT_STATUSES = frozenset({"paid", "no_payment_required"})

# Funnel-signal event types — recorded in the ledger with full attribution
# (email, offer code, client_reference_id) but NEVER advance customer state.
# checkout.session.created fires when a prospect hits a live buy link and
# Stripe opens a checkout session; checkout.session.expired fires when the
# session ages out unpaid (Stripe's default is ~24h). Together they give
# the operator a "started -> completed" ratio per SKU, so a dead buy link
# stops looking indistinguishable from "nobody tried to buy" — an abandon
# proves a prospect clicked; missing abandons means the link isn't working.
_FUNNEL_SIGNAL_EVENT_TYPES = frozenset(
    {
        "checkout.session.created",
        "checkout.session.expired",
    }
)


# Subscription-renewal revenue events. invoice.paid / invoice.payment_succeeded
# carry a recurring payment on an EXISTING subscription (e.g. the Kerry Brown
# $300/mo retainer). They advance no new customer state, but the dollars are
# real and must reach the ledger + decision spine instead of the "ignored"
# catch-all. Subscription-CREATION lands via checkout.session.completed already.
_INVOICE_REVENUE_EVENT_TYPES = frozenset(
    {
        "invoice.paid",
        "invoice.payment_succeeded",
    }
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class WebhookSignatureError(ValueError):
    """Raised when the Stripe-Signature header fails verification."""


# ---------------------------------------------------------------------------
# Signature verification (no Stripe SDK dependency)
# ---------------------------------------------------------------------------


def _parse_sig_header(header: str) -> tuple[int, list[str]]:
    """Parse ``Stripe-Signature: t=<ts>,v1=<hex>,v1=<hex>,...``.

    Returns (timestamp, [v1_signatures]). Raises WebhookSignatureError on a
    malformed header.
    """
    timestamp: int | None = None
    v1_sigs: list[str] = []
    for part in (header or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise WebhookSignatureError(f"bad_timestamp: {value!r}") from exc
        elif key == "v1":
            v1_sigs.append(value)
    if timestamp is None:
        raise WebhookSignatureError("missing_timestamp_in_signature_header")
    if not v1_sigs:
        raise WebhookSignatureError("no_v1_signatures_in_header")
    return timestamp, v1_sigs


def verify_stripe_signature(
    payload_bytes: bytes,
    signature_header: str,
    webhook_secret: str,
    *,
    tolerance_seconds: int | None = None,
    now: float | None = None,
) -> None:
    """Verify a Stripe webhook signature. Raises WebhookSignatureError on any
    failure mode (missing secret, malformed header, expired, mismatched sig).
    Returns None on success.

    Implements Stripe's documented scheme: HMAC-SHA256 of
    ``"{timestamp}.{payload}"`` against the webhook secret, hex-encoded,
    compared against any v1 entry in the header in constant time.
    """
    if not webhook_secret:
        raise WebhookSignatureError("webhook_secret_unset")
    if not signature_header:
        raise WebhookSignatureError("missing_signature_header")

    timestamp, sigs = _parse_sig_header(signature_header)

    # Replay-window check before any HMAC math.
    tolerance = (
        tolerance_seconds if tolerance_seconds is not None else _DEFAULT_REPLAY_TOLERANCE_SECONDS
    )
    current = now if now is not None else time.time()
    # RT FIN-07: the old symmetric abs() check also accepted timestamps up to a
    # full window in the FUTURE. Reject too-old (past > tolerance) and too-future
    # (ahead of a small clock-skew allowance) separately — mirrors the S2
    # asymmetric-freshness convention in common/middleware.py.
    delta = current - timestamp  # positive => event timestamp is in the past
    if delta > tolerance:
        raise WebhookSignatureError(
            f"signature_expired: dt={int(delta)}s > tolerance {tolerance}s",
        )
    if -delta > _STRIPE_FUTURE_SKEW_SECONDS:
        raise WebhookSignatureError(
            f"signature_future: dt={int(-delta)}s ahead > skew {_STRIPE_FUTURE_SKEW_SECONDS}s",
        )

    signed_payload = f"{timestamp}.".encode("utf-8") + payload_bytes
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    for sig in sigs:
        if hmac.compare_digest(expected, sig):
            return
    raise WebhookSignatureError("no_matching_v1_signature")


# ---------------------------------------------------------------------------
# Event log (JSONL on disk, append-only)
# ---------------------------------------------------------------------------


def event_log_path() -> Path:
    return Path(os.getenv("SAMUS_STRIPE_EVENT_LOG", _DEFAULT_EVENT_LOG_PATH))


def _event_ledger() -> persistence.Ledger:
    """Open the Stripe event ledger via the pluggable backend (W-1).

    ``jsonl`` (default — local / VM) appends ``stripe_events.jsonl``;
    ``firestore`` (Cloud Run) appends the ``stripe_events`` collection, whose
    cross-instance conditional ``create()`` is what makes idempotency hold
    once webhook traffic fans out across instances. See
    ``backend.common.persistence.open_ledger``.
    """
    return persistence.open_ledger(
        jsonl_path=str(event_log_path()),
        collection="stripe_events",
    )


# Stripe retries a webhook for up to ~3 days. Bounding the sequential-retry
# scan to a window a comfortable margin past that keeps the per-call set small
# (and bounded) without missing a legitimate retry. The cross-instance /
# concurrent guarantee is ``claim_event_id``, not this scan.
_SEEN_WINDOW_HOURS = float(os.getenv("SAMUS_STRIPE_SEEN_WINDOW_HOURS", "96"))


def _load_seen_event_ids() -> set[str]:
    """Scan the event ledger + return the set of recently-processed Stripe
    event ids. Idempotency check uses this on every inbound webhook so Stripe
    retries (same event_id) become no-ops. This scan catches *sequential*
    retries; concurrent retries are caught by ``claim_event_id``.

    PATCH-RACE-15 — bounded to a TTL window (``_SEEN_WINDOW_HOURS``, default
    96h > Stripe's ~3-day retry horizon) on ``received_at`` so the set does not
    grow with the whole ledger on every call. Records with an unparseable
    ``received_at`` are kept (unknown age = safe, mirrors rotate_by_age).
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_SEEN_WINDOW_HOURS)
    seen: set[str] = set()
    for rec in _event_ledger().scan():
        event_id = rec.get("event_id")
        if not event_id:
            continue
        received_raw = rec.get("received_at") or ""
        if received_raw:
            try:
                received = datetime.strptime(
                    received_raw,
                    "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
                if received < cutoff:
                    continue
            except ValueError:
                pass  # unparseable ts -> keep (safe)
        seen.add(event_id)
    return seen


# PATCH-RACE-15 — retain event records well past the seen-window (and Stripe's
# retry horizon) so rotation never drops a record a legitimate retry still needs,
# while still bounding unbounded ledger growth. Default 30 days.
_EVENT_RETENTION_HOURS = float(
    os.getenv("SAMUS_STRIPE_EVENT_RETENTION_HOURS", str(24 * 30)),
)
# Throttle rotation: roll the file at most once per this many appends per process.
_ROTATE_EVERY_N = int(os.getenv("SAMUS_STRIPE_EVENT_ROTATE_EVERY", "200"))
_append_count = 0


def append_event_record(record: WebhookEventRecord) -> None:
    """Append one WebhookEventRecord to the event ledger. Best-effort."""
    global _append_count
    ledger = _event_ledger()
    try:
        ledger.append(record.model_dump())
    except Exception as exc:  # noqa: BLE001 - best-effort; backend-agnostic
        _LOG.warning("stripe event log append failed: %s", exc)
    # PATCH-RACE-15 — opportunistically bound the ledger. Only the jsonl backend
    # exposes rotate_by_age (Firestore manages its own TTL); throttle so we don't
    # rewrite the file on every append. Best-effort; never blocks the webhook.
    rotate = getattr(ledger, "rotate_by_age", None)
    if rotate is not None and _ROTATE_EVERY_N > 0:
        _append_count += 1
        if _append_count % _ROTATE_EVERY_N == 0:
            try:
                rotate(_EVENT_RETENTION_HOURS, ts_field="received_at")
            except Exception as exc:  # noqa: BLE001 - best-effort maintenance
                _LOG.warning("stripe event ledger rotate failed: %s", exc)


# ---------------------------------------------------------------------------
# Idempotency claim (L1 — TOCTOU fix)
# ---------------------------------------------------------------------------
# The scan-based duplicate check (`_load_seen_event_ids`) and the final
# `append_event_record` are far apart in handle_stripe_webhook: the customer
# mutation, receipt send, and CRM dispatch all happen in between. Two close
# Stripe retries of the same event_id could both pass the scan before either
# appends its record -> double-process (double customer-advance / double
# receipt / double close-the-loop).
#
# `claim_event_id` closes that window by claiming the event_id BEFORE any
# side effect runs, via the ledger backend's atomic claim primitive:
#   - jsonl backend     -> O_CREAT|O_EXCL claim file (one-host atomic)
#   - firestore backend -> conditional create() on a per-event document
#                          (cross-instance atomic — survives Cloud Run
#                          scale-out, which a file claim cannot). This is the
#                          durable W-1 fix for finding L1.

# PATCH-RACE-11 — reservation TTL. A claim is a *reservation* held by whichever
# delivery is actively processing the event; the terminal WebhookEventRecord in
# the ledger is the completion marker. A reservation older than this TTL with NO
# terminal record is presumed orphaned (the owner crashed/OOM'd between claim and
# append) and is reclaimable so the payment is reprocessed instead of being
# permanently dropped. The TTL must comfortably exceed the longest a single
# webhook can legitimately take to run end-to-end (customer mutation + receipt +
# CRM dispatch + auto-fulfill scheduling) so a still-live owner is never stolen
# from. Default 15 minutes; override with SAMUS_STRIPE_CLAIM_TTL_SECONDS.
_CLAIM_TTL_SECONDS = float(os.getenv("SAMUS_STRIPE_CLAIM_TTL_SECONDS", str(15 * 60)))


def _terminal_record_present(event_id: str) -> bool:
    """True iff a terminal WebhookEventRecord for ``event_id`` exists.

    The terminal marker is the completion proof of the reservation+terminal-
    marker scheme: every code path that fully handles an event appends a
    WebhookEventRecord before returning, so its presence means the side effects
    ran to completion. Its ABSENCE (with a held claim) is exactly the
    claim-then-crash window. Best-effort: a scan failure returns True (fail
    toward "completed" = no reclaim) so a transient read error can never trigger
    a double side-effect; the orphaned reservation is simply retried next time.
    """
    if not event_id:
        return False
    try:
        for rec in _event_ledger().scan():
            if rec.get("event_id") == event_id:
                return True
    except Exception as exc:  # noqa: BLE001 - best-effort; see docstring
        _LOG.warning("terminal-record check failed for %s: %s", event_id, exc)
        return True
    return False


def claim_event_id(event_id: str) -> bool:
    """Atomically claim ``event_id`` before processing its side effects.

    Returns True if THIS call won the claim (caller should proceed), False if
    the event id is already owned by another delivery (treat as a duplicate).

    PATCH-RACE-11 — reservation + terminal-marker. The claim is a *reservation*,
    not a permanent terminal marker. Three cases when the atomic claim is lost:

      1. A terminal WebhookEventRecord already exists -> genuinely completed
         -> real duplicate -> return False.
      2. No terminal record, reservation still FRESH (< TTL) -> a concurrent
         delivery is actively processing -> return False (preserve L1
         concurrency idempotency; don't double-process).
      3. No terminal record, reservation EXPIRED (>= TTL) -> the prior owner
         claimed then crashed before completing -> atomically reclaim and
         return True so the payment is reprocessed (closes RACE-11: never
         permanently drop a paid event on claim-then-crash).

    An empty id has nothing to key on; the backend returns True and the caller
    relies on the scan + final append as the only available guards.
    """
    ledger = _event_ledger()
    if ledger.claim(event_id):
        return True
    # Lost the atomic claim. Decide: completed duplicate, live concurrent
    # owner, or orphaned (crashed) reservation we can reclaim.
    if not event_id:
        return False
    if _terminal_record_present(event_id):
        return False  # case 1 — genuinely completed
    age = ledger.claim_age_seconds(event_id)
    if age is None:
        # The reservation vanished between claim() and the age check (e.g. a
        # concurrent reclaim/cleanup). Try one fresh claim; if that also loses,
        # treat as a live concurrent owner.
        return ledger.claim(event_id)
    if age < _CLAIM_TTL_SECONDS:
        return False  # case 2 — fresh reservation, live concurrent owner
    # case 3 — expired reservation, no terminal record: owner crashed. Reclaim.
    if ledger.reclaim_expired(event_id):
        _LOG.warning(
            "reclaimed orphaned stripe webhook reservation event=%s age=%.0fs "
            "(prior owner claimed then never completed; reprocessing)",
            event_id,
            age,
        )
        return True
    # Lost the reclaim race to another reclaimer — they now own it.
    return False


def load_recent_events(window_days: int) -> list[WebhookEventRecord]:
    """Read the event ledger + return events whose ``received_at`` is within
    the last ``window_days``. Used by the morning briefing.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    out: list[WebhookEventRecord] = []
    for rec in _event_ledger().scan():
        try:
            wr = WebhookEventRecord.model_validate(rec)
        except Exception:  # noqa: BLE001
            continue
        try:
            received = datetime.strptime(wr.received_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        if received >= cutoff:
            out.append(wr)
    return out


# ---------------------------------------------------------------------------
# Event payload extraction
# ---------------------------------------------------------------------------


def _extract_invoice_fields(event_data: dict) -> tuple[str, float | None, str, str, str]:
    """From an event.data.object (Stripe invoice) extract:
       (customer_email, amount_paid_usd, currency, subscription_id, billing_reason)
    Returns empties for any non-invoice object.
    """
    inv = event_data.get("object") or {}
    if inv.get("object") != "invoice":
        return "", None, "usd", "", ""
    email = inv.get("customer_email") or ""
    amount_paid = inv.get("amount_paid")
    amount_usd: float | None = None
    if isinstance(amount_paid, (int, float)):
        amount_usd = round(float(amount_paid) / 100.0, 2)
    currency = (inv.get("currency") or "usd").lower()
    sub = inv.get("subscription")
    subscription_id = sub if isinstance(sub, str) else ""
    billing_reason = inv.get("billing_reason") or ""
    return email, amount_usd, currency, subscription_id, billing_reason


def _extract_session_fields(event_data: dict) -> tuple[str, float | None, str, str, str, str]:
    """From an event.data.object (Stripe checkout.session) extract:
    (customer_email, amount_total_usd, currency, hf_offer_code,
     payment_status, customer_website_url)
    """
    session = event_data.get("object") or {}
    if session.get("object") != "checkout.session":
        return "", None, "usd", "", "", ""

    email = ""
    cust_details = session.get("customer_details") or {}
    if isinstance(cust_details, dict):
        email = cust_details.get("email") or ""
    if not email:
        email = session.get("customer_email") or ""

    amount_total = session.get("amount_total")
    amount_usd: float | None = None
    if isinstance(amount_total, (int, float)):
        amount_usd = round(float(amount_total) / 100.0, 2)
    currency = (session.get("currency") or "usd").lower()
    payment_status = session.get("payment_status") or ""

    # hf_offer_code lives in session.metadata (set on the payment_link), and
    # is also mirrored to the subscription/payment_intent for some flows.
    md = session.get("metadata") or {}
    hf_offer = md.get("hf_offer_code") or ""

    # Customer-supplied website URL (Win #2). Stripe checkout custom_fields
    # surface here. We look for key='website_url' specifically; payment-link
    # owners can configure additional fields but only this one drives
    # auto-fulfill today.
    website_url = ""
    for cf in session.get("custom_fields") or []:
        if not isinstance(cf, dict):
            continue
        if cf.get("key") == "website_url" and cf.get("type") == "text":
            text_obj = cf.get("text") or {}
            website_url = (text_obj.get("value") or "").strip()
            break

    return email, amount_usd, currency, hf_offer, payment_status, website_url


def _extract_subscription_fields(event_data: dict) -> tuple[str, str, str, str]:
    """Cut 2 — extract subscription-related session fields.

    Returns (session_mode, subscription_id, subscription_price_id,
    client_reference_id). All strings; empty when the field is missing.

    Notes:
      - ``session.mode`` is 'payment' for one-time audit purchases and
        'subscription' for monthly add-ons. We dispatch on this to advance
        state to 'renewed' instead of 'paid'.
      - ``session.subscription`` is the sub_xxx id (only set in subscription
        mode). For a follow-on Stripe API call to expand items, prod will
        fetch separately — here we just record the id.
      - ``subscription_price_id`` is best-effort: Stripe nests price ids on
        line_items.data[*].price.id but only when the session is retrieved
        with expand[]=line_items. We also look at subscription_data.metadata
        for our own hf_target_price_id key (operator-set on the payment link)
        and fall back to '' if neither is present.
      - ``client_reference_id`` is Cut 3's attribution column. We extract +
        store it so the Cut 3 agent can wire mark_converted() later without
        re-parsing the JSONL log.
    """
    session = event_data.get("object") or {}
    if session.get("object") != "checkout.session":
        return "", "", "", ""

    mode = session.get("mode") or ""
    subscription_id = session.get("subscription") or ""
    if not isinstance(subscription_id, str):
        subscription_id = ""  # Stripe sometimes embeds the full sub object
    client_reference_id = session.get("client_reference_id") or ""

    # Best-effort price id extraction. Try line_items first (only present
    # when the session was retrieved with expand=line_items, e.g. some
    # webhook flows where Stripe inlines them), then subscription_data
    # metadata (operator can set 'hf_target_price_id' on the payment link).
    price_id = ""
    line_items = session.get("line_items") or {}
    if isinstance(line_items, dict):
        for item in line_items.get("data") or []:
            if not isinstance(item, dict):
                continue
            price = item.get("price") or {}
            if isinstance(price, dict) and price.get("id"):
                price_id = price["id"]
                break
    if not price_id:
        sub_data = session.get("subscription_data") or {}
        if isinstance(sub_data, dict):
            md = sub_data.get("metadata") or {}
            if isinstance(md, dict):
                price_id = md.get("hf_target_price_id") or ""

    return mode, subscription_id, price_id, client_reference_id


# ---------------------------------------------------------------------------
# Auto-fulfill scheduling (Win #2)
# ---------------------------------------------------------------------------


def _run_auto_fulfill_sync(
    *, email: str, audit_url: str, event_id: str, fulfill_fn: Any = None
) -> None:
    """Run fulfill_customer + append a follow-up JSONL record with the outcome.

    Best-effort: any exception is caught, logged, and surfaced via a
    failure-status record so the operator can see it in the morning briefing.
    This function is the thread target — see _schedule_auto_fulfill.
    """
    if fulfill_fn is None:
        from backend.fulfill import fulfill_customer as _real_fulfill

        fulfill_fn = _real_fulfill

    ok = False
    message_id = ""
    err = ""
    try:
        result = fulfill_fn(
            email=email,
            audit_url=audit_url,
            send_email=True,
        )
        ok = bool(getattr(result, "ok", False))
        message_id = getattr(result, "email_message_id", "") or ""
        if not ok:
            # FulfillmentResult carries per-step detail; pick the first failed
            # step's detail as the error summary.
            for step in getattr(result, "steps", []) or []:
                if getattr(step, "status", "") == "failed":
                    err = f"{step.name}: {step.detail}"
                    break
            if not err:
                err = "fulfill returned ok=False with no failed step"
    except Exception as exc:  # noqa: BLE001 — best-effort path
        _LOG.warning("auto_fulfill_crashed event=%s: %s", event_id, exc)
        err = f"unexpected: {exc!r}"

    # Append a second record so the morning briefing can correlate by event_id.
    follow_up = WebhookEventRecord(
        event_id=event_id,
        event_type="auto_fulfill",
        received_at=iso_now(),
        livemode=False,  # auto-fulfill outcome isn't bound to live/test mode
        customer_email=email,
        customer_website_url=audit_url,
        process_status="processed" if ok else "failed",
        process_note=(
            f"auto_fulfill completed (message_id={message_id})"
            if ok
            else f"auto_fulfill failed: {err}"
        ),
        auto_fulfill_attempted=True,
        auto_fulfill_ok=ok,
        auto_fulfill_message_id=message_id,
        auto_fulfill_error=err,
    )
    append_event_record(follow_up)


def _schedule_auto_fulfill(*, email: str, audit_url: str, event_id: str) -> None:
    """Spawn a daemon thread to run auto-fulfill. Returns immediately.

    Tests monkeypatch this to a synchronous shim so they can assert on the
    outcome without race conditions.
    """
    t = threading.Thread(
        target=_run_auto_fulfill_sync,
        kwargs={"email": email, "audit_url": audit_url, "event_id": event_id},
        name=f"auto_fulfill_{event_id}",
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Close-the-loop CRM dispatch (Phase 5)
# ---------------------------------------------------------------------------
# When a payment lands the operator wants the corresponding open opportunity
# advanced to closed_won. Two attribution paths feed the same CRM close-out:
#   - client_reference_id is an Opportunity id ("op_..."): the buy link was
#     tagged with the exact deal — e.g. a hand-dialed prospect was sent a
#     tagged purchase link. Close THAT opportunity directly; no email lookup,
#     so it works regardless of which email the customer paid with.
#   - otherwise: fall back to the email -> contact -> prospect -> open
#     opportunity lookup.
# Both enqueue a best-effort CRM job via the gateway. Stripe retries non-2xx,
# so a CRM hiccup MUST NOT bubble up here — every failure logs + drops.


def _dispatch_crm_job(
    action: str,
    payload: dict[str, Any],
    *,
    event_id: str,
) -> None:
    """Best-effort enqueue of one CRM-worker job via the gateway's
    ``POST /dispatch/crm`` (gateway routes to the ``samus-crm-jobs`` SQS queue).

    Sync, not a daemon thread — the webhook handler stays alive for the
    duration of the call (~tens of ms), so the enqueue survives Cloud Run
    scale-to-zero. A daemon thread spawned outside a tracked request can be
    killed when the instance scales down between requests.

    Never raises, never blocks Stripe — Stripe always sees a 200 and we never
    trigger a retry storm over a CRM hiccup.
    """
    settings = get_settings()
    # Defensive getattr — finance tests stub a minimal Settings object that
    # may not carry these attributes; an unset value behaves like unset.
    gateway_urls = getattr(settings, "gateway_urls", None) or {}
    gateway_url = gateway_urls.get("gateway") if hasattr(gateway_urls, "get") else None
    if not gateway_url:
        _LOG.debug("finance crm dispatch skipped (%s): gateway_url_unset", action)
        return
    if not getattr(settings, "shared_hmac_key", ""):
        _LOG.debug("finance crm dispatch skipped (%s): shared_hmac_key_unset", action)
        return

    envelope = {
        "task_id": event_id,
        "payload": payload,
        "metadata": {"action": action},
    }
    try:
        signed_post_json_sync(gateway_url, "/dispatch/crm", envelope, retries=2)
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks webhook
        _LOG.warning(
            "finance crm dispatch failed (%s) for event=%s: %s",
            action,
            event_id,
            exc,
        )


def _dispatch_close_the_loop_to_crm(
    *,
    email: str,
    amount_usd: float,
    event_id: str,
) -> None:
    """Email-attribution close-the-loop: enqueue a ``close_payment_to_opportunity``
    job — the CRM worker does the email->opportunity lookup AND the
    advance-to-closed_won in one atomic message.
    """
    if not email:
        return
    _dispatch_crm_job(
        "close_payment_to_opportunity",
        {
            "email": email,
            "amount_usd": float(amount_usd or 0.0),
            "payment_ref": event_id,
        },
        event_id=event_id,
    )


def _dispatch_close_opportunity_by_id(
    *,
    opportunity_id: str,
    amount_usd: float,
    event_id: str,
    customer_email: str = "",
) -> None:
    """Exact-attribution close-the-loop: the buy link carried the Opportunity
    id in ``client_reference_id``, so enqueue a ``close_opportunity_from_payment``
    job that closes that specific deal — no email lookup needed.

    ``customer_email`` is forwarded to the CRM worker so it can verify the
    opportunity belongs to the paying customer (IDOR guard, CWE-639).
    """
    if not opportunity_id:
        return
    _dispatch_crm_job(
        "close_opportunity_from_payment",
        {
            "opportunity_id": opportunity_id,
            "won_amount_usd": float(amount_usd or 0.0),
            "payment_ref": event_id,
            "customer_email": customer_email,
        },
        event_id=event_id,
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def handle_stripe_webhook(
    payload_bytes: bytes,
    signature_header: str,
    *,
    customer_store: Any = None,
    now: float | None = None,
) -> WebhookProcessResult:
    """End-to-end webhook processor.

    Verifies signature -> parses event -> idempotency check -> handles by type.
    For ``checkout.session.completed``: creates/advances the customer to
    'paid' in the memory workcell and appends to the JSONL log.

    ``customer_store`` is injectable so tests can pass a fake.
    """
    settings = get_settings()
    # Defensive: support both the typed Settings field (stripe_webhook_secret)
    # and a bare STRIPE_WEBHOOK_SECRET env var. Parallel work to the shared
    # config.py may add/remove the typed field; env var is the durable contract.
    secret = (
        getattr(settings, "stripe_webhook_secret", None)
        or os.getenv("STRIPE_WEBHOOK_SECRET", "")
        or ""
    ).strip()
    # Signature verification — raises WebhookSignatureError on any failure.
    # Caller (FastAPI endpoint) translates that to HTTP 400.
    verify_stripe_signature(payload_bytes, signature_header, secret, now=now)

    # Parse the event body.
    try:
        body = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return WebhookProcessResult(
            received=True,
            process_status="bad_payload",
            note=f"json_decode_failed: {exc}",
        )
    try:
        event = StripeWebhookEvent.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        return WebhookProcessResult(
            received=True,
            process_status="bad_payload",
            note=f"event_validation_failed: {exc}",
        )

    # Idempotency check: Stripe retries the same event_id; we skip after the
    # first successful append. This JSONL scan catches *sequential* retries
    # (a retry that arrives after the first delivery already finished).
    if event.id and event.id in _load_seen_event_ids():
        return WebhookProcessResult(
            received=True,
            process_status="duplicate",
            event_id=event.id,
            event_type=event.type,
            note="event_id already in log",
        )

    # L1 — atomic idempotency RESERVATION. The JSONL scan above is a non-atomic
    # check-then-act: two *concurrent* retries of the same event can both pass
    # it before either appends its record. Reserve the event_id here, BEFORE any
    # side effect (customer mutation / receipt / CRM dispatch). A lost claim
    # means another delivery already owns it -> duplicate. PATCH-RACE-11: the
    # claim is a reclaimable reservation, not a permanent terminal marker — if a
    # prior owner claimed then crashed before writing its WebhookEventRecord,
    # claim_event_id() reclaims the expired reservation here and reprocesses, so
    # a claim-then-crash never permanently drops a paid event.
    if not claim_event_id(event.id):
        return WebhookProcessResult(
            received=True,
            process_status="duplicate",
            event_id=event.id,
            event_type=event.type,
            note="event_id already claimed (concurrent delivery)",
        )

    # Finding 4 (defense-in-depth) — reject test-mode events in production.
    # A test-mode (livemode=false) checkout.session.completed must not be
    # able to advance a real customer to "paid" or trigger a receipt on the
    # production deployment. The gate is strictly env-scoped: it only fires
    # when the runtime env is production AND the setting is on, so the test
    # suite (which uses livemode=False fixtures in a non-production env)
    # still processes events normally. The event is still recorded so the
    # rejection is visible to the operator; no customer state is mutated.
    settings_for_gate = settings
    if (
        getattr(settings_for_gate, "stripe_reject_test_mode_in_production", True)
        and getattr(settings_for_gate, "is_production", False)
        and not event.livemode
    ):
        record = WebhookEventRecord(
            event_id=event.id,
            event_type=event.type,
            received_at=iso_now(),
            livemode=event.livemode,
            process_status="ignored",
            process_note="test-mode event rejected in production env",
        )
        append_event_record(record)
        _LOG.warning(
            "stripe webhook rejected test-mode event in production: id=%s type=%s",
            event.id,
            event.type,
        )
        return WebhookProcessResult(
            received=True,
            process_status="ignored",
            event_id=event.id,
            event_type=event.type,
            note="test-mode event rejected in production env",
        )

    # Funnel-signal events (checkout.session.created / expired). Recorded with
    # full attribution so the morning briefing can compute started-vs-completed
    # ratios per SKU and surface abandonment tagged to the outreach op that
    # sourced the click. NEVER advances customer state — only completed does.
    if event.type in _FUNNEL_SIGNAL_EVENT_TYPES:
        email, amount_usd, currency, hf_offer, payment_status, website_url = (
            _extract_session_fields(event.data)
        )
        session_mode, subscription_id, subscription_price_id, client_reference_id = (
            _extract_subscription_fields(event.data)
        )
        record = WebhookEventRecord(
            event_id=event.id,
            event_type=event.type,
            received_at=iso_now(),
            livemode=event.livemode,
            customer_email=email,
            amount_total_usd=amount_usd,
            currency=currency,
            hf_offer_code=hf_offer,
            payment_status=payment_status,
            customer_website_url=website_url,
            session_mode=session_mode,
            subscription_id=subscription_id,
            subscription_price_id=subscription_price_id,
            client_reference_id=client_reference_id,
            process_status="funnel_signal",
            process_note=f"{event.type} recorded; no state advance",
        )
        append_event_record(record)
        return WebhookProcessResult(
            received=True,
            process_status="funnel_signal",
            event_id=event.id,
            event_type=event.type,
            customer_email=email,
            customer_website_url=website_url,
            session_mode=session_mode,
            subscription_id=subscription_id,
            note=f"{event.type} recorded; no state advance",
        )

    # Subscription renewals (invoice.paid / invoice.payment_succeeded) carry real
    # recurring revenue on an EXISTING subscription. Record with the dollar amount
    # and emit payment.received so the renewal reaches revenue tracking, instead of
    # dropping into the valueless "ignored" catch-all below.
    if event.type in _INVOICE_REVENUE_EVENT_TYPES:
        (
            email,
            amount_usd,
            currency,
            subscription_id,
            billing_reason,
        ) = _extract_invoice_fields(event.data)
        record = WebhookEventRecord(
            event_id=event.id,
            event_type=event.type,
            received_at=iso_now(),
            livemode=event.livemode,
            customer_email=email,
            amount_total_usd=amount_usd,
            currency=currency,
            payment_status="paid",
            session_mode="subscription",
            subscription_id=subscription_id,
            process_status="renewal",
            process_note=f"{event.type} recorded (billing_reason={billing_reason})",
        )
        append_event_record(record)
        if amount_usd:
            from backend.common.business_events import (
                PAYMENT_RECEIVED,
                emit_business_event,
            )

            emit_business_event(
                PAYMENT_RECEIVED,
                workcell="finance",
                revenue_usd=float(amount_usd or 0.0),
                metadata={
                    "stripe_event_id": event.id,
                    "subscription_id": subscription_id,
                    "currency": currency,
                    "billing_reason": billing_reason,
                    "customer_email_tail": (email or "")[-12:],
                },
            )
        return WebhookProcessResult(
            received=True,
            process_status="renewal",
            event_id=event.id,
            event_type=event.type,
            customer_email=email,
            session_mode="subscription",
            subscription_id=subscription_id,
            note=f"{event.type} recorded (billing_reason={billing_reason})",
        )

    # Only checkout.session.completed actually drives state today.
    if event.type != "checkout.session.completed":
        record = WebhookEventRecord(
            event_id=event.id,
            event_type=event.type,
            received_at=iso_now(),
            livemode=event.livemode,
            process_status="ignored",
            process_note="event type not handled",
        )
        append_event_record(record)
        return WebhookProcessResult(
            received=True,
            process_status="ignored",
            event_id=event.id,
            event_type=event.type,
            note="event type not handled",
        )

    # Extract customer + offer info from the session.
    (
        email,
        amount_usd,
        currency,
        hf_offer,
        payment_status,
        website_url,
    ) = _extract_session_fields(event.data)
    # Cut 2: subscription-mode dispatch fields. Always extracted (cheap), used
    # to branch below. ``session_mode`` defaults to 'payment' when Stripe
    # doesn't include the mode key (older test fixtures).
    (
        session_mode,
        subscription_id,
        subscription_price_id,
        client_reference_id,
    ) = _extract_subscription_fields(event.data)

    if not email:
        record = WebhookEventRecord(
            event_id=event.id,
            event_type=event.type,
            received_at=iso_now(),
            livemode=event.livemode,
            amount_total_usd=amount_usd,
            currency=currency,
            hf_offer_code=hf_offer,
            payment_status=payment_status,
            customer_website_url=website_url,
            session_mode=session_mode,
            subscription_id=subscription_id,
            process_status="failed",
            process_note="no customer_email in checkout.session",
        )
        append_event_record(record)
        return WebhookProcessResult(
            received=True,
            process_status="failed",
            event_id=event.id,
            event_type=event.type,
            customer_website_url=website_url,
            session_mode=session_mode,
            subscription_id=subscription_id,
            note="no customer_email in checkout.session",
        )

    # Lazy import so test fixtures can inject the store without dragging Neo4j
    # into the module-load chain.
    if customer_store is None:
        from backend.memory.customers import CustomerStore

        customer_store = CustomerStore()

    is_subscription = session_mode == "subscription"
    # Cut 2: MRR contribution. For monthly subscriptions the first invoice
    # equals the monthly price, so amount_total/100 is a faithful proxy.
    # Annual plans would over-report MRR — out of scope for Cut 2 (we ship
    # only monthly add-ons today). amount_usd is already in dollars.
    subscription_mrr_usd: float | None = amount_usd if is_subscription and amount_usd else None

    customer_id = ""
    advanced_to = ""
    try:
        existing = customer_store.get_by_email(email)
        if existing is None:
            customer = customer_store.create_customer(
                email=email,
                source="stripe_webhook",
                # NOTE: metadata={} on purpose — Neo4j rejects Map property values
                # ("Property values can only be of primitive types or arrays"),
                # so passing {"hf_offer_code": ...} crashes create_customer's
                # MERGE. The hf_offer_code is captured in the WebhookEventRecord
                # below, so no information is lost by dropping it here. A proper
                # fix would JSON-encode metadata at the customers.py layer
                # (separate cross-cutting concern).
            )
        else:
            customer = existing
        customer_id = customer.id

        if is_subscription:
            # Cut 2 — subscription branch. Advance delivered -> renewed when
            # an audit buyer upgrades to the monthly. For any other prior
            # state (prospect / paid / brand-new-subscriber) we record the
            # event for visibility but do NOT mutate state — that path would
            # collide with the audit fulfillment lifecycle. A separate Cut
            # later may model "subscription-only" customers explicitly.
            if customer.current_state == "delivered" and (
                payment_status in _FULFILL_PAYMENT_STATUSES
            ):
                event_state = customer_store.advance_state(
                    customer_id=customer.id,
                    to_state="renewed",
                    reason=(
                        f"stripe subscription started sub={subscription_id} (event={event.id})"
                    ),
                )
                advanced_to = event_state.to_state
            elif customer.current_state == "delivered":
                # RT FIN-01: funds not cleared (e.g. ACH-pending/failed sub).
                # Withhold the renewed advance but still record the event below.
                advanced_to = customer.current_state  # no-op transition
                _LOG.warning(
                    "subscription_renew_withheld_pending_payment event=%s payment_status=%s",
                    event.id,
                    payment_status,
                )
            else:
                advanced_to = customer.current_state  # no-op transition
                _LOG.warning(
                    "subscription_for_non_delivered_customer email=%s "
                    "current_state=%s sub=%s event=%s",
                    email,
                    customer.current_state,
                    subscription_id,
                    event.id,
                )
        else:
            # Existing payment-mode behavior. Only advance if not already past
            # 'paid' — webhook is the truth source for the paid transition;
            # later states (in_delivery/delivered/renewed) are owned by the
            # fulfill orchestrator and shouldn't be reverted.
            if customer.current_state in ("prospect", "contacted") and (
                payment_status in _FULFILL_PAYMENT_STATUSES
            ):
                event_state = customer_store.advance_state(
                    customer_id=customer.id,
                    to_state="paid",
                    reason=f"stripe checkout.session.completed (event={event.id})",
                )
                advanced_to = event_state.to_state
            elif customer.current_state in ("prospect", "contacted"):
                # RT FIN-01: payment not cleared (ACH-pending/failed). Withhold
                # the 'paid' advance — fulfillment must not fire before funds
                # settle — but fall through to record the event for visibility.
                advanced_to = customer.current_state  # no-op transition
                _LOG.warning(
                    "fulfillment_withheld_pending_payment event=%s payment_status=%s",
                    event.id,
                    payment_status,
                )
            else:
                advanced_to = customer.current_state  # no-op transition
    except Exception as exc:  # noqa: BLE001
        record = WebhookEventRecord(
            event_id=event.id,
            event_type=event.type,
            received_at=iso_now(),
            livemode=event.livemode,
            customer_email=email,
            amount_total_usd=amount_usd,
            currency=currency,
            hf_offer_code=hf_offer,
            payment_status=payment_status,
            customer_website_url=website_url,
            session_mode=session_mode,
            subscription_id=subscription_id,
            subscription_mrr_usd=subscription_mrr_usd,
            process_status="failed",
            process_note=f"customer_store_failure: {exc}",
        )
        append_event_record(record)
        return WebhookProcessResult(
            received=True,
            process_status="failed",
            event_id=event.id,
            event_type=event.type,
            customer_email=email,
            customer_website_url=website_url,
            session_mode=session_mode,
            subscription_id=subscription_id,
            subscription_mrr_usd=subscription_mrr_usd,
            note=f"customer_store_failure: {exc}",
        )

    received_at = iso_now()
    # Best-effort receipt send for one-time (payment-mode) purchases only.
    # Stripe sends its own recurring receipts for subscriptions; sending ours
    # too would double-up on the customer's inbox.
    receipt: dict[str, Any] = {"sent": False, "message_id": "", "error": ""}
    if not is_subscription:
        receipt = send_payment_receipt(
            customer_email=email,
            amount_total_usd=amount_usd,
            currency=currency,
            hf_offer_code=hf_offer,
            event_id=event.id,
            received_at=received_at,
        )

    # Win #2: auto-fulfill scheduling. Only for one-time (payment-mode)
    # sessions — subscriptions don't have a deliverable to auto-fulfill.
    # Three conditions must all be true for payment-mode:
    #   - offer code is in the operator's whitelist (SAMUS_AUTO_FULFILL_OFFERS)
    #   - the customer supplied a website URL via the custom_fields prompt
    #   - we have a real customer_id (i.e. the advance to 'paid' succeeded)
    # The thread itself is fire-and-forget — its outcome lands in a second
    # JSONL row (event_type='auto_fulfill') that morning briefing correlates
    # back to this row by event_id.
    # Defensive getattr — parallel config evolution may add/remove the
    # typed field; an unset attribute behaves like an empty whitelist.
    whitelist = getattr(settings, "auto_fulfill_offer_codes", None) or []
    # Validate website_url at intake before it reaches the fulfill daemon.
    # website_url is customer-supplied (Stripe checkout custom_field) — it must
    # be a reachable public HTTPS/HTTP URL before we enqueue it. Validation
    # here is defence-in-depth: safe_fetch.safe_get() guards the actual fetch
    # too, but catching a bad URL here avoids a daemon thread that immediately
    # fails and produces a confusing 'auto_fulfill' error row in the JSONL log.
    _website_url_valid = False
    if website_url:
        try:
            from backend.common.safe_fetch import assert_public_http_url, SsrfBlockedError

            assert_public_http_url(website_url)
            _website_url_valid = True
        except (SsrfBlockedError, ValueError) as _ssrf_exc:
            _LOG.warning(
                "stripe webhook auto_fulfill skipped: website_url failed SSRF "
                "validation event=%s url_tail=%s reason=%s",
                event.id,
                website_url[-40:],
                _ssrf_exc,
            )
    # PATCH-FIN-D7-02 — paid auto-fulfill must be backed by a real, settled,
    # USD payment at/above the floor. Exclude no_payment_required ($0 coupon /
    # free-trial sessions): they advance the state machine but must NOT trigger
    # fulfillment of a paid deliverable. Reject non-USD (an off-currency total
    # cannot be compared to the USD-denominated floor) and any amount below the
    # configured minimum. Fail closed — the event is still recorded below.
    _min_offer_floor_usd = float(
        os.getenv("SAMUS_AUTO_FULFILL_MIN_USD", "1.0"),
    )
    _payment_qualifies = (
        payment_status == "paid"
        and currency == "usd"
        and (amount_usd or 0.0) >= _min_offer_floor_usd
    )
    if not _payment_qualifies and not is_subscription and hf_offer and hf_offer in whitelist:
        _LOG.warning(
            "auto_fulfill withheld: payment does not qualify event=%s "
            "payment_status=%s currency=%s amount_usd=%s floor=%s",
            event.id,
            payment_status,
            currency,
            amount_usd,
            _min_offer_floor_usd,
        )
    auto_fulfill_scheduled = bool(
        not is_subscription
        and hf_offer
        and hf_offer in whitelist
        and _website_url_valid
        and customer_id
        and _payment_qualifies
    )
    if auto_fulfill_scheduled:
        _schedule_auto_fulfill(
            email=email,
            audit_url=website_url,
            event_id=event.id,
        )

    if is_subscription:
        note = f"subscription started; advanced to {advanced_to}"
        if subscription_id:
            note += f" sub={subscription_id}"
    else:
        note = f"advanced to {advanced_to}"
        if auto_fulfill_scheduled:
            note += "; auto_fulfill scheduled"

    record = WebhookEventRecord(
        event_id=event.id,
        event_type=event.type,
        received_at=received_at,
        livemode=event.livemode,
        customer_email=email,
        customer_id=customer_id,
        amount_total_usd=amount_usd,
        currency=currency,
        hf_offer_code=hf_offer,
        payment_status=payment_status,
        process_status="processed",
        process_note=note,
        receipt_sent=receipt["sent"],
        receipt_message_id=receipt["message_id"],
        receipt_error=receipt["error"],
        customer_website_url=website_url,
        session_mode=session_mode,
        subscription_id=subscription_id,
        subscription_price_id=subscription_price_id,
        subscription_mrr_usd=subscription_mrr_usd,
        client_reference_id=client_reference_id,
        auto_fulfill_attempted=auto_fulfill_scheduled,
    )
    append_event_record(record)

    # Unified business-event ledger (HOTL Tranche 1) — one payment.received
    # row per settled checkout, with revenue + whatever attribution the
    # client_reference_id carries (op_<opportunity> / out_<prospect>). Fail-soft
    # by contract: emit_business_event never raises, so it can never block the
    # 200 back to Stripe.
    if payment_status in _FULFILL_PAYMENT_STATUSES:
        from backend.common.business_events import (
            PAYMENT_RECEIVED,
            emit_business_event,
        )

        _ref = client_reference_id or ""
        emit_business_event(
            PAYMENT_RECEIVED,
            workcell="finance",
            prospect_id=(_ref.removeprefix("out_") if _ref.startswith("out_") else None),
            opportunity_id=(_ref if _ref.startswith("op_") else None),
            revenue_usd=(amount_usd or 0.0),
            metadata={
                "stripe_event_id": event.id,
                "hf_offer_code": hf_offer,
                "currency": currency,
                "session_mode": session_mode,
                "customer_email_tail": (email or "")[-12:],
            },
        )

    # Phase 5 — close-the-loop CRM dispatch on a successful payment. When the
    # buy link was tagged with client_reference_id=op_<id> (a hand-dialed
    # prospect sent a tagged purchase link), close THAT exact Opportunity.
    # Otherwise fall back to email-based attribution. Both require a
    # customer_id (the customer-store advance succeeded). Best-effort — never
    # blocks the 200 we return to Stripe.
    if customer_id:
        if client_reference_id.startswith("op_"):
            _dispatch_close_opportunity_by_id(
                opportunity_id=client_reference_id,
                amount_usd=amount_usd or 0.0,
                event_id=event.id,
                customer_email=email,
            )
        elif email:
            _dispatch_close_the_loop_to_crm(
                email=email,
                amount_usd=amount_usd or 0.0,
                event_id=event.id,
            )

    # Outreach conversion attribution — a cold-outreach flyer buy-now link
    # carries client_reference_id=out_<prospect_id>. Credit the exact prospect
    # (and offer) so campaign ROI is measurable regardless of the paying email.
    # Best-effort + idempotent; never blocks the 200 to Stripe.
    if client_reference_id.startswith("out_") and (payment_status in _FULFILL_PAYMENT_STATUSES):
        try:
            from backend.finance.outreach_attribution import record_conversion

            # Campaign attribution (HOTL Tranche 1): payment links tagged with
            # metadata.hf_campaign_id credit the exact campaign that fired the
            # flyer. Absent metadata -> None (un-attributed, as before).
            _session_md = ((event.data or {}).get("object") or {}).get("metadata") or {}
            record_conversion(
                ref=client_reference_id,
                email=email,
                amount_usd=amount_usd or 0.0,
                currency=currency,
                offer_code=hf_offer,
                event_id=event.id,
                received_at=received_at,
                campaign_id=(str(_session_md.get("hf_campaign_id") or "") or None),
            )
        except Exception as exc:  # noqa: BLE001 — never fail webhook on attribution
            _LOG.warning(
                "outreach attribution failed (ref=%s event=%s): %s",
                client_reference_id,
                event.id,
                exc,
            )

    # Cut 3 — close the upsell attribution loop. When a subscription session
    # comes in with client_reference_id='upsell_<queue_event_id>', look up
    # the originating upsell touch + write a 'converted' transition row.
    # This is what makes morning briefing's "Upsells (Nd): X converted"
    # count attribute correctly. Best-effort; a parse failure or missing
    # queue row just leaves the count unattributed.
    if is_subscription and client_reference_id.startswith("upsell_") and subscription_id:
        try:
            from backend.finance.upsell_queue import (
                _read_all_rows as _upsell_read,
                mark_converted as _upsell_mark_converted,
            )

            queue_event_id = client_reference_id.removeprefix("upsell_")
            # Find the touch that issued this URL so we can credit the right
            # cadence position. Walk the queue and find the 'queued' row with
            # this event_id; its touch_num tells us which beat closed.
            matched_touch_num: int | None = None
            matched_customer_id: str = ""
            matched_customer_email: str = ""
            for r in _upsell_read():
                if r.event_id == queue_event_id and r.kind == "queued":
                    matched_touch_num = r.touch_num
                    matched_customer_id = r.customer_id
                    matched_customer_email = r.customer_email
                    break
            # RT FIN-09: the Stripe payer must be the customer the touch was
            # issued to. Without this, anyone who learns a queue_event_id can set
            # client_reference_id=upsell_<id> on their own subscription and credit
            # (corrupt the attribution of) another customer's upsell touch.
            payer_matches = bool(
                email
                and matched_customer_email
                and email.strip().lower() == matched_customer_email.strip().lower()
            )
            if matched_touch_num is not None and matched_customer_id and payer_matches:
                _upsell_mark_converted(
                    customer_id=matched_customer_id,
                    source_offer_code="seo_audit",  # only audit upsell wired
                    touch_num=matched_touch_num,
                    subscription_id=subscription_id,
                )
            else:
                _LOG.info(
                    "upsell attribution: no queued row found for event_id=%s "
                    "(stripe client_reference_id=%s); subscription_id=%s",
                    queue_event_id,
                    client_reference_id,
                    subscription_id,
                )
        except Exception as exc:  # noqa: BLE001 — never fail webhook on attribution
            _LOG.warning(
                "upsell attribution failed (sub=%s ref=%s): %s",
                subscription_id,
                client_reference_id,
                exc,
            )

    # Deal-closed stimulus -> CODB investment reasoner. A closed sale/new
    # subscription frees up cash; surface a fresh, ranked recommendation for
    # which CODB scale-up to buy next while there is cash to act on. Gated
    # internally by SAMUS_CODB_REASONER_ENABLED (wire-not-arm; a no-op when
    # disarmed) and RECOMMEND-ONLY (emits guidance records, never spends).
    # Best-effort — a reasoner fault never fails the 200 we return to Stripe.
    if customer_id and (payment_status in _FULFILL_PAYMENT_STATUSES):
        try:
            from backend.cognitive.codb_reasoner import on_deal_closed

            on_deal_closed(
                mrr_delta_usd=float(subscription_mrr_usd or 0.0),
                amount_usd=float(amount_usd or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 — never fail webhook on reasoner
            _LOG.warning("codb reasoner stimulus failed (event=%s): %s", event.id, exc)

    return WebhookProcessResult(
        received=True,
        process_status="processed",
        event_id=event.id,
        event_type=event.type,
        customer_email=email,
        customer_id=customer_id,
        customer_advanced_to=advanced_to,
        customer_website_url=website_url,
        auto_fulfill_scheduled=auto_fulfill_scheduled,
        session_mode=session_mode,
        subscription_id=subscription_id,
        subscription_mrr_usd=subscription_mrr_usd,
        note=note,
    )
