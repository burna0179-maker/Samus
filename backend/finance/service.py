"""Finance service — pull Stripe + load CODB + compute runway, audit it all."""

from __future__ import annotations

import logging
import os

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from . import (
    actions as actions_mod,
    billing,
    codb,
    debts as debts_mod,
    declines,
    hardship as hardship_mod,
    info_gaps as info_gaps_mod,
    liabilities,
    webhook as webhook_mod,
)
from .models import (
    ActionsSummary,
    CodbSummary,
    CustomerBillingSummary,
    CustomerChargeRow,
    CustomerSubscriptionRow,
    DebtPortfolioSummary,
    DeclinesSummary,
    FinanceSnapshot,
    HardshipContext,
    InfoGapsSummary,
    LiabilitiesSummary,
    MrrAddDigest,
    MrrAddSummary,
    PaymentLinksRollup,
    RecentPaymentsSummary,
    RunwayCalc,
    SnapshotRequest,
    StripeBalance,
    UpsellRowDigest,
    UpsellSummary,
    WebhookProcessResult,
)
from .stripe_client import (
    StripeClient,
    StripeError,
    monthly_recurring_revenue_usd,
)


_LOG = logging.getLogger("samus.finance.service")
_AUDIT_PATH_DEFAULT = "/opt/samus/data/finance/finance_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(
        os.getenv("SAMUS_FINANCE_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def _new_stripe_client() -> StripeClient | None:
    """Build a StripeClient from settings. Returns None when key is missing."""
    settings = get_settings()
    key = (settings.stripe_api_key or "").strip()
    if not key:
        return None
    return StripeClient(api_key=key)


def _record_revenue_foresight(
    *,
    mrr_usd: float,
    monthly_burn_usd: float,
    monthly_target_usd: float,
    runway_days: float | None,
    available_usd: float,
) -> None:
    """ECO-ADR-0003 §7 shadow: append a revenue Cone of Plausibility to the
    finance audit ledger. Observation-only — it reads the snapshot figures and
    records a futures report; it changes no balance, MRR, or decision. Fail-open
    so a foresight fault never affects the finance path."""
    try:
        from _shared.foresight import revenue_cone

        cone = revenue_cone(
            mrr_usd=float(mrr_usd or 0.0),
            monthly_burn_usd=float(monthly_burn_usd or 0.0),
            monthly_target_usd=float(monthly_target_usd or 0.0),
            runway_days=runway_days,
            available_usd=float(available_usd or 0.0),
        )
        ev = events.build_audit_event(
            service="finance",
            task_id="revenue_foresight",
            action="revenue_foresight_shadow",
            input_payload={
                "mrr_usd": mrr_usd,
                "monthly_burn_usd": monthly_burn_usd,
                "monthly_target_usd": monthly_target_usd,
                "runway_days": runway_days,
            },
            output_payload=cone.report().to_dict(),
            status="completed",
        )
        _audit_ledger().append(ev)
    except Exception as exc:  # noqa: BLE001 — shadow must never affect finance
        _LOG.warning("finance foresight shadow failed: %s", exc)


def _build_codb_summary(ts: str) -> CodbSummary:
    return codb.summarize(codb.load_registry(), ts)


def _build_liabilities_summary(ts: str) -> LiabilitiesSummary:
    return liabilities.summarize(liabilities.load_registry(), ts)


def _build_declines_summary(ts: str, window_days: int = 30) -> DeclinesSummary:
    return declines.summarize(declines.load_registry(), window_days=window_days, ts=ts)


def _build_debt_portfolio(ts: str) -> DebtPortfolioSummary:
    registry, loaded = debts_mod.load_registry()
    return debts_mod.summarize(registry, loaded, ts)


def _build_actions_summary(ts: str) -> ActionsSummary:
    registry, loaded = actions_mod.load_registry()
    return actions_mod.summarize(registry, loaded, ts=ts)


def _build_info_gaps_summary(ts: str) -> InfoGapsSummary:
    registry, loaded = info_gaps_mod.load_registry()
    return info_gaps_mod.summarize(registry, loaded, ts)


def _build_hardship_context() -> HardshipContext:
    return hardship_mod.load_context()


def _charges_total_usd_dollars(charges) -> float:
    cents = sum(c.amount for c in charges if c.paid and c.currency.lower() == "usd")
    return round(cents / 100.0, 2)


def _payouts_total_usd_dollars(payouts) -> float:
    cents = sum(p.amount for p in payouts if p.currency.lower() == "usd")
    return round(cents / 100.0, 2)


def get_snapshot(req: SnapshotRequest) -> FinanceSnapshot:
    """Pull Stripe + load CODB + compute runway; return the composite snapshot.

    Cache key includes the limits so distinct callers don't collide.
    Stripe-side failures are tolerated: the snapshot still returns CODB +
    runway-with-zero-balance and surfaces ``stripe_error`` to the caller.
    """
    cache_key = f"finance.snapshot:{req.charges_limit}:{req.payouts_limit}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info(
            "snapshot cache hit charges_limit=%s payouts_limit=%s",
            req.charges_limit,
            req.payouts_limit,
        )
        return FinanceSnapshot.model_validate(cached)

    ts = iso_now()
    registry = codb.load_registry()
    codb_summary = codb.summarize(registry, ts)
    liabilities_summary = _build_liabilities_summary(ts)
    declines_summary = _build_declines_summary(ts)
    debt_portfolio = _build_debt_portfolio(ts)
    actions_summary = _build_actions_summary(ts)
    info_gaps_summary = _build_info_gaps_summary(ts)
    hardship_context = _build_hardship_context()

    balance: StripeBalance | None = None
    charges_count = 0
    charges_total = 0.0
    payouts_count = 0
    payouts_total = 0.0
    subs_count = 0
    mrr_usd = 0.0
    stripe_reachable = False
    stripe_error: str | None = None

    client = _new_stripe_client()
    if client is None:
        stripe_error = "stripe_api_key_unset"
    else:
        try:
            balance = client.fetch_balance()
            charges = client.fetch_charges(limit=req.charges_limit)
            payouts = client.fetch_payouts(limit=req.payouts_limit)
            # Live recurring revenue: any active subscription on the account,
            # not just ones that arrived via the webhook pipeline.
            subs = client.fetch_subscriptions(status="active", limit=100)
            charges_count = len(charges)
            charges_total = _charges_total_usd_dollars(charges)
            payouts_count = len(payouts)
            payouts_total = _payouts_total_usd_dollars(payouts)
            subs_count = len(subs)
            mrr_usd = monthly_recurring_revenue_usd(subs)
            stripe_reachable = True
        except StripeError as exc:
            # LEAK-FIN-MRR — store an opaque token; the raw StripeError text
            # may carry account/request internals and stripe_error can be
            # serialized to a response. Keep the detail server-side only.
            stripe_error = "stripe_error"
            _LOG.warning("stripe ingest failed: %s", exc)

    # Real cash: Stripe available auto-sweeps to the bank (~$0), so prefer the
    # Found business-bank balance (running sum of its activity export) when we
    # have it. That single substitution makes BOTH the runway alert and the
    # downstream affordability/conserve gate reason on real cash, not the $0
    # artifact. Falls back to Stripe available when no Found export is present.
    _stripe_available_usd = balance.available_usd_dollars() if balance else 0.0
    from .found_cash import best_available_cash_usd

    available_usd, _cash_source = best_available_cash_usd(_stripe_available_usd)
    runway = codb.compute_runway(
        available_balance_usd=available_usd,
        total_monthly_burn_usd=codb_summary.total_monthly_burn_usd,
        runway_alert_days=registry.revenue_targets.runway_alert_days,
    )

    snapshot = FinanceSnapshot(
        ts=ts,
        stripe_reachable=stripe_reachable,
        stripe_error=stripe_error,
        balance=balance,
        recent_charges_count=charges_count,
        recent_charges_usd_total=charges_total,
        recent_payouts_count=payouts_count,
        recent_payouts_usd_total=payouts_total,
        active_subscriptions_count=subs_count,
        mrr_usd=mrr_usd,
        codb_summary=codb_summary,
        runway=runway,
        liabilities_summary=liabilities_summary,
        declines_summary=declines_summary,
        debt_portfolio=debt_portfolio,
        actions_summary=actions_summary,
        info_gaps_summary=info_gaps_summary,
        hardship_context=hardship_context,
    )

    ev = events.build_audit_event(
        service="finance",
        task_id="snapshot",
        action="snapshot",
        input_payload=req.model_dump(),
        output_payload={
            "stripe_reachable": stripe_reachable,
            "available_usd": available_usd,
            "mrr_usd": mrr_usd,
            "active_subscriptions_count": subs_count,
            "monthly_burn_usd": codb_summary.total_monthly_burn_usd,
            "days_of_runway": runway.days_of_runway,
            "alert_triggered": runway.alert_triggered,
            "outstanding_liabilities_usd": liabilities_summary.total_outstanding_usd,
            "cash_distress": declines_summary.cash_distress,
            "debt_confirmed_total_usd": debt_portfolio.confirmed_total_usd,
            "actions_overdue": actions_summary.overdue_count,
            "actions_due_today": actions_summary.due_today_count,
            "info_gaps_critical_open": len(info_gaps_summary.critical_open),
        },
        status="completed" if stripe_reachable else "degraded",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("finance audit append failed: %s", exc)

    # ECO-ADR-0003 §7 shadow: revenue Cone of Plausibility (flag-gated,
    # observation-only — never changes the snapshot or any finance decision).
    if getattr(get_settings(), "foresight_enabled", False):
        _record_revenue_foresight(
            mrr_usd=mrr_usd,
            monthly_burn_usd=codb_summary.total_monthly_burn_usd,
            monthly_target_usd=registry.revenue_targets.monthly_minimum_usd,
            runway_days=runway.days_of_runway,
            available_usd=available_usd,
        )

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, snapshot.model_dump())
    return snapshot


def get_codb_summary() -> CodbSummary:
    """Standalone CODB view — no Stripe call, always available."""
    return _build_codb_summary(iso_now())


def get_liabilities_summary() -> LiabilitiesSummary:
    """Standalone liabilities view — loans owed minus repayments per lender."""
    return _build_liabilities_summary(iso_now())


def get_declines_summary(window_days: int = 30) -> DeclinesSummary:
    """Standalone declines view + cash-distress flag over the given window."""
    return _build_declines_summary(iso_now(), window_days=window_days)


def get_debt_portfolio() -> DebtPortfolioSummary:
    """Standalone debt portfolio view (Phase 3)."""
    return _build_debt_portfolio(iso_now())


def get_actions_summary() -> ActionsSummary:
    """Standalone action calendar — buckets vs today (Phase 3)."""
    return _build_actions_summary(iso_now())


def get_info_gaps_summary() -> InfoGapsSummary:
    """Standalone info-gap summary (Phase 3)."""
    return _build_info_gaps_summary(iso_now())


def get_hardship_context() -> HardshipContext:
    """Standalone hardship context (Phase 3)."""
    return _build_hardship_context()


def get_payment_links(*, active: bool = True) -> PaymentLinksRollup:
    """Standalone payment-link rollup (Phase 2 read-only).

    Pulls live links from Stripe and returns them sorted livemode-first,
    samus-managed-next, offer-code-alphabetical-last. Stripe errors degrade
    gracefully via stripe_reachable=False.
    """
    return billing.fetch_rollup(iso_now(), active=active)


def handle_stripe_webhook(payload_bytes: bytes, signature_header: str) -> WebhookProcessResult:
    """Phase 8 — Stripe webhook endpoint orchestrator.

    Verifies signature, parses event, advances customer state on
    checkout.session.completed, appends to the JSONL event log. Raises
    WebhookSignatureError on signature failure (caller maps to HTTP 400).
    """
    return webhook_mod.handle_stripe_webhook(payload_bytes, signature_header)


def get_recent_payments(window_days: int = 7) -> RecentPaymentsSummary:
    """Phase 8 — rollup of recent Stripe webhook events for the briefing.

    Reads the JSONL event log and counts events by process_status. Returns
    log_loaded=False when no log file exists yet.
    """
    log_path = webhook_mod.event_log_path()
    log_loaded = log_path.exists()
    events = webhook_mod.load_recent_events(window_days) if log_loaded else []
    # Split: Stripe-source events (checkout.session.completed etc) vs the
    # Win #2 auto_fulfill follow-up records the handler appends from the
    # background thread.
    stripe_events = [e for e in events if e.event_type != "auto_fulfill"]
    auto_events = [e for e in events if e.event_type == "auto_fulfill"]
    processed = sum(1 for e in stripe_events if e.process_status == "processed")
    ignored = sum(1 for e in stripe_events if e.process_status == "ignored")
    failed = sum(1 for e in stripe_events if e.process_status in ("failed", "bad_payload"))
    # Correlate auto_fulfill follow-ups back to their originating event_id so
    # the awaiting count drops when the background thread succeeded.
    auto_ok_event_ids = {e.event_id for e in auto_events if e.auto_fulfill_ok}
    auto_fail_event_ids = {e.event_id for e in auto_events if not e.auto_fulfill_ok}
    # Awaiting fulfillment: 'processed' events where neither auto-fulfill nor
    # a manual Run-Fulfill has completed. Operator's Run-Fulfill is idempotent
    # so over-surfacing the manual path is harmless; under-surfacing would
    # leave a paid customer in limbo.
    awaiting = sum(
        1
        for e in stripe_events
        if e.process_status == "processed" and e.event_id not in auto_ok_event_ids
    )
    return RecentPaymentsSummary(
        window_days=window_days,
        total_count=len(stripe_events),
        processed_count=processed,
        ignored_count=ignored,
        failed_count=failed,
        awaiting_fulfillment_count=awaiting,
        auto_fulfilled_count=len(auto_ok_event_ids),
        auto_fulfill_failed_count=len(auto_fail_event_ids),
        # Surface stripe events to the briefing; auto_fulfill rows are
        # internal correlation rows, not operator-facing line items.
        recent_events=sorted(
            stripe_events,
            key=lambda e: e.received_at,
            reverse=True,
        ),
        ts=iso_now(),
        log_loaded=log_loaded,
    )


def get_mrr_adds(window_days: int = 7) -> MrrAddSummary:
    """Cut 2 — rollup of new subscription adds for the morning briefing.

    Reads the WebhookEventRecord JSONL log and filters to subscription-mode
    sessions with a non-empty ``subscription_id`` and positive
    ``subscription_mrr_usd`` within the window. Independent of Stripe's
    live MRR endpoint so a Stripe outage doesn't hide a fresh signup —
    pairs with the existing MRR line in the briefing (which calls Stripe).
    """
    log_path = webhook_mod.event_log_path()
    log_loaded = log_path.exists()
    events_in_window = webhook_mod.load_recent_events(window_days) if log_loaded else []
    adds: list[MrrAddDigest] = []
    total = 0.0
    for ev in events_in_window:
        if not ev.subscription_id:
            continue
        if ev.subscription_mrr_usd is None or ev.subscription_mrr_usd <= 0:
            continue
        adds.append(
            MrrAddDigest(
                customer_email=ev.customer_email,
                subscription_id=ev.subscription_id,
                mrr_usd=ev.subscription_mrr_usd,
                ts=ev.received_at,
            )
        )
        total += ev.subscription_mrr_usd
    adds.sort(key=lambda r: r.ts, reverse=True)
    return MrrAddSummary(
        window_days=window_days,
        added_count=len(adds),
        total_mrr_usd=round(total, 2),
        recent_adds=adds,
        ts=iso_now(),
    )


def get_upsell_summary(window_days: int = 14) -> UpsellSummary:
    """Roll up the upsell_queue.jsonl ledger for the morning briefing.

    'queued' / 'sent' / 'failed' / 'converted' / 'skipped_dup' counts are
    derived from the LATEST row per (customer_id, source_offer_code,
    touch_num) — same fold-the-log pattern as the webhook event log.

    ``due_now_count`` is the subset of queued whose ``due_at <= now``
    (i.e. what the next runner pass will actually send).
    """
    from datetime import datetime, timedelta, timezone
    from .upsell_queue import (
        _current_state_by_key,
        _parse_iso,
        _read_all_rows,
        queue_path,
    )

    path = queue_path()
    log_loaded = path.exists()
    if not log_loaded:
        return UpsellSummary(
            window_days=window_days,
            queued_count=0,
            due_now_count=0,
            sent_count=0,
            failed_count=0,
            converted_count=0,
            skipped_dup_count=0,
            recent_sent=[],
            recent_failed=[],
            ts=iso_now(),
            log_loaded=False,
        )

    all_rows = _read_all_rows()
    latest = _current_state_by_key(all_rows)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    queued = due_now = sent = failed = converted = skipped_dup = 0
    recent_sent: list[UpsellRowDigest] = []
    recent_failed: list[UpsellRowDigest] = []
    for row in latest.values():
        row_ts = _parse_iso(row.ts)
        in_window = row_ts is not None and row_ts >= cutoff
        if row.kind == "queued":
            # Count all queued regardless of window — they're future-facing.
            queued += 1
            due_at = _parse_iso(row.due_at)
            if due_at is not None and due_at <= now:
                due_now += 1
            continue
        if not in_window:
            continue
        if row.kind == "sent":
            sent += 1
            recent_sent.append(
                UpsellRowDigest(
                    customer_email=row.customer_email,
                    touch_num=row.touch_num,
                    kind=row.kind,
                    source_offer_code=row.source_offer_code,
                    target_offer_code=row.target_offer_code,
                    ts=row.ts,
                )
            )
        elif row.kind == "failed":
            failed += 1
            recent_failed.append(
                UpsellRowDigest(
                    customer_email=row.customer_email,
                    touch_num=row.touch_num,
                    kind=row.kind,
                    source_offer_code=row.source_offer_code,
                    target_offer_code=row.target_offer_code,
                    ts=row.ts,
                )
            )
        elif row.kind == "converted":
            converted += 1
        elif row.kind == "skipped_dup":
            skipped_dup += 1

    return UpsellSummary(
        window_days=window_days,
        queued_count=queued,
        due_now_count=due_now,
        sent_count=sent,
        failed_count=failed,
        converted_count=converted,
        skipped_dup_count=skipped_dup,
        recent_sent=sorted(recent_sent, key=lambda r: r.ts, reverse=True)[:5],
        recent_failed=sorted(recent_failed, key=lambda r: r.ts, reverse=True)[:5],
        ts=iso_now(),
        log_loaded=True,
    )


def _charge_to_row(charge) -> CustomerChargeRow:
    """Convert a StripeCharge into the per-customer summary row shape."""
    from datetime import datetime, timezone

    created_iso = (
        datetime.fromtimestamp(
            int(charge.created or 0),
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    return CustomerChargeRow(
        charge_id=charge.id,
        amount_usd=round(int(charge.amount or 0) / 100.0, 2),
        currency=charge.currency,
        status=charge.status,
        paid=bool(charge.paid),
        created_iso=created_iso,
        description=(charge.description or "")[:240],
    )


def _subscription_to_row(sub) -> CustomerSubscriptionRow:
    """Convert a StripeSubscription into the per-customer summary row shape."""
    price_ids = [item.price.id for item in sub.items if item.price]
    mrr = monthly_recurring_revenue_usd([sub])
    return CustomerSubscriptionRow(
        subscription_id=sub.id,
        status=sub.status,
        mrr_usd=mrr,
        price_ids=price_ids,
    )


def get_customer_billing_summary(
    email: str,
    *,
    charges_limit: int = 5,
) -> CustomerBillingSummary:
    """Per-customer billing snapshot for the inbound-email handler.

    Workflow: Stripe customers?email -> pick most-recent livemode row ->
    fetch active subscriptions for that customer id -> fetch recent
    charges for that customer id -> assemble.

    Tolerant by design: an unset api key, an unknown email, or a Stripe
    transport hiccup all return a typed CustomerBillingSummary (with
    ``state`` reflecting which case). Never raises — the calling
    inbound-email handler must still create the CRM artifact even when
    billing context is unavailable.
    """
    ts = iso_now()
    email_clean = (email or "").strip().lower()

    client = _new_stripe_client()
    if client is None:
        return CustomerBillingSummary(
            email=email_clean,
            state="lookup_failed",
            ts=ts,
            lookup_error="stripe_api_key_unset",
        )

    try:
        customers = client.fetch_customers_by_email(email_clean, limit=10)
    except StripeError as exc:
        _LOG.warning("customer lookup failed for %s: %s", email_clean[-12:], exc)
        return CustomerBillingSummary(
            # LEAK-FIN-MRR — opaque token; raw StripeError stays in the log.
            email=email_clean,
            state="lookup_failed",
            ts=ts,
            lookup_error="lookup_failed",
        )

    if not customers:
        return CustomerBillingSummary(
            email=email_clean,
            state="unknown",
            ts=ts,
        )

    # Stripe can return multiple rows per email (legacy duplicates +
    # test/live mixups). Pick the most-recent livemode row when present,
    # else the most-recent overall. Keep the full id list for audit.
    customers_sorted = sorted(
        customers,
        key=lambda c: (c.livemode, c.created),
        reverse=True,
    )
    primary = customers_sorted[0]
    customer_ids = [c.id for c in customers_sorted]

    subs: list = []
    charges: list = []
    error_note = ""
    try:
        subs = client.fetch_subscriptions(
            status="active",
            limit=20,
            customer=primary.id,
        )
    except StripeError as exc:
        # LEAK-FIN-MRR — opaque note; raw StripeError stays in the log.
        error_note = "subscriptions_failed"
        _LOG.warning("subscription lookup failed for %s: %s", primary.id, exc)

    try:
        charges = client.fetch_charges(
            limit=max(1, min(100, int(charges_limit))),
            customer=primary.id,
        )
    except StripeError as exc:
        # Non-fatal: surface in error_note but keep returning what we have.
        # LEAK-FIN-MRR — opaque note; raw StripeError stays in the log.
        error_note = f"{error_note}; charges_failed" if error_note else "charges_failed"
        _LOG.warning("charge lookup failed for %s: %s", primary.id, exc)

    charge_rows = [_charge_to_row(c) for c in charges]
    sub_rows = [_subscription_to_row(s) for s in subs]
    total_paid = round(
        sum(c.amount_usd for c in charge_rows if c.paid),
        2,
    )
    mrr_total = round(sum(s.mrr_usd for s in sub_rows), 2)

    if sub_rows:
        state = "subscriber"
    elif charge_rows:
        state = "customer"
    else:
        # Customer exists in Stripe but no charges + no subs — could be a
        # checkout-abandoned signup. Still 'unknown' for billing-posture
        # purposes; the customer_id is preserved so the operator can dig in.
        state = "unknown"

    summary = CustomerBillingSummary(
        email=email_clean,
        state=state,
        stripe_customer_id=primary.id,
        stripe_customer_ids=customer_ids,
        livemode=primary.livemode,
        total_paid_usd=total_paid,
        mrr_usd=mrr_total,
        recent_charges=charge_rows,
        active_subscriptions=sub_rows,
        lookup_error=error_note,
        ts=ts,
    )

    ev = events.build_audit_event(
        service="finance",
        task_id=f"customer_summary:{primary.id}",
        action="customer_summary",
        input_payload={"email_tail": email_clean[-12:]},
        output_payload={
            "state": state,
            "customer_id": primary.id,
            "n_subscriptions": len(sub_rows),
            "n_charges": len(charge_rows),
            "mrr_usd": mrr_total,
            "total_paid_usd": total_paid,
            "lookup_error": error_note,
        },
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("finance audit append failed: %s", exc)

    return summary


def confirm_customer_exists(stripe_customer_id: str) -> bool:
    """Confirm a Stripe customer id maps to a live customer (FIN-08).

    Called by ``POST /customer-exists`` for the voice workcell, which uses the
    answer to decide whether it may meter a receptionist's calls. The contract
    is strictly fail-closed: return ``True`` ONLY when Stripe affirmatively
    confirms a live customer with this id. Everything else — a blank/malformed
    id, an unset Stripe key, a 404, a deleted customer, or any Stripe
    error/transport failure — returns ``False`` so the voice side disables
    metering (the safe direction: never bill an unverified id).

    Never raises.
    """
    cid = (stripe_customer_id or "").strip()
    if not cid:
        return False

    client = _new_stripe_client()
    if client is None:
        # No Stripe key configured -> we cannot confirm -> fail closed.
        _LOG.warning("customer-exists check: stripe_api_key_unset")
        return False

    try:
        customer = client.retrieve_customer(cid)
    except StripeError as exc:
        # Auth / rate-limit / transport / 5xx -> cannot confirm -> fail closed.
        _LOG.warning("customer-exists check failed for %s: %s", cid, exc)
        return False
    except Exception as exc:  # noqa: BLE001 — defensive: never confirm on error
        _LOG.warning("customer-exists check error for %s: %s", cid, exc)
        return False

    return customer is not None


def get_runway(override_balance_usd: float | None = None) -> RunwayCalc:
    """Runway endpoint — uses live Stripe balance unless ``override`` is set."""
    registry = codb.load_registry()
    total_burn = codb.total_monthly_burn(registry.costs)

    if override_balance_usd is not None:
        balance_usd = float(override_balance_usd)
    else:
        client = _new_stripe_client()
        stripe_available = 0.0
        if client is not None:
            try:
                stripe_available = client.fetch_balance().available_usd_dollars()
            except StripeError as exc:
                _LOG.warning("stripe balance fetch failed: %s", exc)
        # Prefer the real Found bank balance over Stripe available (auto-swept ~$0).
        from .found_cash import best_available_cash_usd

        balance_usd, _ = best_available_cash_usd(stripe_available)

    return codb.compute_runway(
        available_balance_usd=balance_usd,
        total_monthly_burn_usd=total_burn,
        runway_alert_days=registry.revenue_targets.runway_alert_days,
    )
