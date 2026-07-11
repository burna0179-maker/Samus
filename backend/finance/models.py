"""Pydantic models for the finance workcell."""
from __future__ import annotations

from datetime import date as _date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Stripe shapes (only the fields Phase 1 reads — everything else is dropped)
# ---------------------------------------------------------------------------

class StripeBalanceLine(BaseModel):
    """One currency slice of a Stripe balance bucket (available / pending)."""
    model_config = ConfigDict(extra="ignore")

    amount: int  # smallest currency unit (cents for USD)
    currency: str


class StripeBalance(BaseModel):
    """Decoded /v1/balance response. Amounts in smallest currency unit."""
    model_config = ConfigDict(extra="ignore")

    available: list[StripeBalanceLine] = Field(default_factory=list)
    pending: list[StripeBalanceLine] = Field(default_factory=list)
    livemode: bool = False

    def available_usd_dollars(self) -> float:
        """Sum the USD available balance, converted to dollars."""
        cents = sum(l.amount for l in self.available if l.currency.lower() == "usd")
        return round(cents / 100.0, 2)

    def pending_usd_dollars(self) -> float:
        cents = sum(l.amount for l in self.pending if l.currency.lower() == "usd")
        return round(cents / 100.0, 2)


class StripeCharge(BaseModel):
    """One row of /v1/charges. Just the fields the snapshot displays."""
    model_config = ConfigDict(extra="ignore")

    id: str
    amount: int
    currency: str
    status: str
    paid: bool
    created: int  # unix epoch seconds
    description: str | None = None
    customer: str | None = None


class StripePayout(BaseModel):
    """One row of /v1/payouts. Used for cash-leaving-Stripe accounting."""
    model_config = ConfigDict(extra="ignore")

    id: str
    amount: int
    currency: str
    status: str
    arrival_date: int  # unix epoch seconds (date payout lands at bank)
    created: int


class StripeRecurring(BaseModel):
    """The ``recurring`` sub-object on a Stripe price."""
    model_config = ConfigDict(extra="ignore")

    interval: str = "month"  # day | week | month | year
    interval_count: int = 1


class StripeSubscriptionItemPrice(BaseModel):
    """Subset of a Stripe price object embedded in a subscription item."""
    model_config = ConfigDict(extra="ignore")

    id: str
    unit_amount: int = 0  # smallest currency unit
    currency: str = "usd"
    recurring: StripeRecurring = Field(default_factory=StripeRecurring)


class StripeSubscriptionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    quantity: int = 1
    price: StripeSubscriptionItemPrice


class StripeSubscription(BaseModel):
    """Subset of a Stripe subscription. Only fields used for MRR rollup."""
    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    items: list[StripeSubscriptionItem] = Field(default_factory=list)

    @classmethod
    def model_validate_envelope(cls, raw: dict) -> "StripeSubscription":
        """Stripe nests items as {data: [...]}; flatten before validating."""
        items_data = (raw.get("items") or {}).get("data") or []
        return cls.model_validate({
            "id": raw.get("id"),
            "status": raw.get("status", ""),
            "items": items_data,
        })


class StripeCustomer(BaseModel):
    """Subset of a Stripe customer record used by per-customer lookups.

    Just the fields the billing-summary surface reads: identity + creation
    time for picking the most-recent row when an email maps to multiple
    Customer objects. Stripe sends amounts in cents; we surface dollars
    only at the summary layer.
    """
    model_config = ConfigDict(extra="ignore")

    id: str
    email: str | None = None
    name: str | None = None
    created: int = 0      # unix epoch seconds
    livemode: bool = False
    delinquent: bool = False


class StripePaymentLink(BaseModel):
    """Subset of a Stripe payment_link. Only the fields the workcell renders."""
    model_config = ConfigDict(extra="ignore")

    id: str
    url: str
    active: bool = True
    livemode: bool = False
    currency: str = "usd"
    is_subscription: bool = False  # derived: subscription_data is not None
    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def model_validate_envelope(cls, raw: dict) -> "StripePaymentLink":
        """Flatten Stripe shape: derive ``is_subscription`` from ``subscription_data``."""
        return cls.model_validate({
            "id": raw.get("id"),
            "url": raw.get("url", ""),
            "active": bool(raw.get("active", True)),
            "livemode": bool(raw.get("livemode", False)),
            "currency": raw.get("currency", "usd"),
            "is_subscription": raw.get("subscription_data") is not None,
            "metadata": raw.get("metadata") or {},
        })


# ---------------------------------------------------------------------------
# Billing / payment-link surfaces (read-only Phase 2)
# ---------------------------------------------------------------------------

class PaymentLinkSummary(BaseModel):
    """One row in the operator-facing payment-link list."""
    model_config = ConfigDict(extra="forbid")

    id: str
    offer_code: str  # from metadata.hf_offer_code, '' if missing
    url: str
    is_subscription: bool
    livemode: bool
    samus_managed: bool  # from metadata.samus_managed == 'true'


class PaymentLinksRollup(BaseModel):
    """Aggregate /payment_links response."""
    model_config = ConfigDict(extra="forbid")

    links: list[PaymentLinkSummary] = Field(default_factory=list)
    count_total: int
    count_subscription: int
    count_one_time: int
    livemode_count: int
    stripe_reachable: bool
    stripe_error: str | None = None
    ts: str


# ---------------------------------------------------------------------------
# Stripe webhook surfaces (Phase 8)
# ---------------------------------------------------------------------------

WebhookProcessStatus = Literal[
    "processed",     # event recognized + customer advanced
    "ignored",       # event type not handled (return 200 so Stripe stops retrying)
    "duplicate",     # idempotency hit on Stripe event_id
    "bad_payload",   # malformed event body
    "failed",        # recognized event type but downstream advance failed
    # Funnel observability — event recognized as a checkout-lifecycle signal
    # (started / abandoned) that carries useful attribution (email, offer,
    # client_reference_id) but is NEVER a state-advancing signal. The record
    # lands in the ledger so morning briefing can surface abandonment rate,
    # and so a dead buy link stops looking indistinguishable from "nobody
    # tried to buy": abandons prove intent even when nothing completes.
    "funnel_signal",
    # Subscription renewal — invoice.paid / invoice.payment_succeeded. Revenue
    # was received (recurring), but no NEW customer state advances (the sub
    # already exists). Recorded with the dollar amount so renewals are visible
    # in the ledger / decision spine instead of dropped as "ignored".
    "renewal",
]


class StripeWebhookEvent(BaseModel):
    """Subset of a Stripe Event object used by the webhook handler."""
    model_config = ConfigDict(extra="ignore")

    id: str          # Stripe event id (used for idempotency)
    type: str        # e.g. 'checkout.session.completed'
    livemode: bool = False
    created: int = 0
    data: dict = Field(default_factory=dict)


class WebhookEventRecord(BaseModel):
    """One row of the operator-facing webhook event log (JSONL on disk)."""
    model_config = ConfigDict(extra="forbid")

    event_id: str            # Stripe's evt_... id
    event_type: str
    received_at: str         # ISO-8601 UTC, our clock
    livemode: bool
    customer_email: str = ""
    customer_id: str = ""    # memory workcell customer id (if created/found)
    amount_total_usd: float | None = None
    currency: str = "usd"
    hf_offer_code: str = ""  # from session.metadata.hf_offer_code if present
    payment_status: str = ""
    process_status: WebhookProcessStatus
    process_note: str = ""
    # Receipt-send outcome (best-effort; failure does NOT fail the webhook).
    # Defaults preserve backward-compat for rows written before the receipt
    # feature shipped.
    receipt_sent: bool = False
    receipt_message_id: str = ""
    receipt_error: str = ""
    # Customer-provided URL from Stripe checkout custom_fields (Win #2).
    # Required for auto-fulfill — without it, even a whitelisted offer
    # stays operator-triggered. Defaults preserve backward-compat.
    customer_website_url: str = ""
    # Auto-fulfill outcome. The initial webhook record carries
    # auto_fulfill_attempted=True iff a background thread was spawned;
    # the thread's actual outcome lands in a SEPARATE follow-up record
    # (event_type='auto_fulfill') so the JSONL stays append-only and
    # the morning briefing can correlate the two by event_id.
    auto_fulfill_attempted: bool = False
    auto_fulfill_ok: bool = False
    auto_fulfill_message_id: str = ""
    auto_fulfill_error: str = ""
    # Cut 2: subscription checkout fields. Stripe fires checkout.session.completed
    # for both payment-mode (audit) and subscription-mode (SEO Optimization)
    # sessions; we now dispatch on session.mode. Defaults preserve backward-
    # compat for rows written before the subscription branch shipped.
    session_mode: str = ""             # 'payment' | 'subscription' | 'setup'
    subscription_id: str = ""          # populated for mode='subscription'
    subscription_price_id: str = ""    # first item's price id (best-effort)
    subscription_mrr_usd: float | None = None  # monthly recurring revenue $
    # Cut 3 attribution passthrough — extracted from session.client_reference_id
    # but NOT acted on here. The Cut 3 agent reads this column to call
    # upsell_queue.mark_converted() against the matching queued touch.
    client_reference_id: str = ""


class WebhookProcessResult(BaseModel):
    """Return shape from handle_stripe_webhook()."""
    model_config = ConfigDict(extra="forbid")

    received: bool
    process_status: WebhookProcessStatus
    event_id: str = ""
    event_type: str = ""
    customer_email: str = ""
    customer_id: str = ""
    customer_advanced_to: str = ""
    note: str = ""
    # Win #2: surface the customer-supplied URL + whether auto-fulfill was
    # scheduled, so the FastAPI route / smoke-test scripts can confirm the
    # branch fired without scraping the JSONL log.
    customer_website_url: str = ""
    auto_fulfill_scheduled: bool = False
    # Cut 2: surface subscription details so the smoke-test / FastAPI route
    # can confirm the subscription branch fired without scraping the JSONL.
    session_mode: str = ""
    subscription_id: str = ""
    subscription_mrr_usd: float | None = None


# ---------------------------------------------------------------------------
# Metered usage reporting (AI Digital Receptionist — Stripe Meter)
# ---------------------------------------------------------------------------

class MeterReportResult(BaseModel):
    """Return shape from ``report_call_minutes()`` — a Stripe meter-event push.

    ``report_call_minutes`` NEVER raises: a metering failure must not fail the
    voice webhook, so the caller inspects ``ok`` / ``error`` instead of
    catching. ``minutes_reported`` is the whole-minute (rounded-up) value sent
    to the Stripe Meter; ``meter_event_id`` is Stripe's id on success.
    """
    model_config = ConfigDict(extra="forbid")

    ok: bool
    call_id: str
    stripe_customer_id: str = ""
    minutes_reported: int = 0
    meter_event_id: str = ""
    error: str = ""
    ts: str


class MeterEventRequest(BaseModel):
    """``POST /meter-event`` body — report one call's usage to a Stripe Meter.

    Posted by the voice workcell after an inbound receptionist call ends.
    The route resolves the SKU's Stripe Meter and pushes a meter event;
    ``call_id`` doubles as the Stripe idempotency identifier.
    """
    model_config = ConfigDict(extra="forbid")

    stripe_customer_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    sku_id: str = "retainer_ai_receptionist"


class UpsellRowDigest(BaseModel):
    """One row surfaced in the briefing — minimal subset of UpsellQueueRow."""
    model_config = ConfigDict(extra="forbid")

    customer_email: str
    touch_num: int
    kind: str
    source_offer_code: str
    target_offer_code: str = ""
    ts: str


class UpsellSummary(BaseModel):
    """Rollup of the upsell_queue.jsonl for the morning briefing.

    Counts derived from the append-only ledger: latest row per
    (customer_id, source_offer_code, touch_num) key determines the
    'current state' that gets counted. ``converted_count`` will be 0
    until Cut 2 (subscription webhook handler) lands.
    """
    model_config = ConfigDict(extra="forbid")

    window_days: int
    queued_count: int        # currently-queued touches (any due date)
    due_now_count: int       # subset of queued whose due_at <= now
    sent_count: int          # touches whose latest row in window is 'sent'
    failed_count: int        # touches whose latest row in window is 'failed'
    converted_count: int     # touches whose latest row in window is 'converted'
    skipped_dup_count: int   # touches whose latest row in window is 'skipped_dup'
    recent_sent: list[UpsellRowDigest]  # most-recent sent rows (max 5)
    recent_failed: list[UpsellRowDigest]  # most-recent failed rows (max 5)
    ts: str
    log_loaded: bool


class MrrAddDigest(BaseModel):
    """One subscription-add row surfaced in the briefing."""
    model_config = ConfigDict(extra="forbid")

    customer_email: str
    subscription_id: str
    mrr_usd: float
    ts: str  # received_at of the originating webhook row


class MrrAddSummary(BaseModel):
    """Rollup of new subscription adds for the morning briefing.

    Cut 2 — derived from the WebhookEventRecord JSONL log: rows where
    ``subscription_id`` is non-empty and ``subscription_mrr_usd > 0`` within
    the window. Independent of Stripe's live MRR endpoint so an outage
    doesn't hide a fresh signup.
    """
    model_config = ConfigDict(extra="forbid")

    window_days: int
    added_count: int
    total_mrr_usd: float
    recent_adds: list[MrrAddDigest]
    ts: str


class RecentPaymentsSummary(BaseModel):
    """Rollup of recent webhook events for the morning briefing."""
    model_config = ConfigDict(extra="forbid")

    window_days: int
    total_count: int
    processed_count: int
    ignored_count: int
    failed_count: int
    awaiting_fulfillment_count: int  # processed + customer at 'paid' (not delivered)
    recent_events: list[WebhookEventRecord]
    ts: str
    log_loaded: bool
    # Win #2 auto-fulfill follow-up counts (event_type='auto_fulfill' rows).
    # auto_fulfilled_count means the background thread reported ok=True;
    # auto_fulfill_failed_count means it reported a failure (operator-fixable).
    # Both decay the awaiting count: success removes the row from the queue,
    # failure flags it for operator-manual re-run.
    auto_fulfilled_count: int = 0
    auto_fulfill_failed_count: int = 0


# ---------------------------------------------------------------------------
# CODB registry shapes (YAML-seeded)
# ---------------------------------------------------------------------------

CodbCriticality = Literal["critical", "high", "medium", "low"]


class CodbItem(BaseModel):
    """One line in the CODB registry — a recurring cost of doing business."""
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    category: str  # infrastructure / ai / data / payments / labor / other
    criticality: CodbCriticality
    estimated_monthly_usd: float
    notes: str = ""


class CodbRevenueTargets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_minimum_usd: float = 0.0
    runway_alert_days: int = 60


class CodbInvestmentOption(BaseModel):
    """One SCALABLE CODB investment — an optional upgrade the reasoner ranks.

    Distinct from :class:`CodbItem` (current recurring burn): an investment is
    an OPTIONAL scale-up that relieves a named operational bottleneck. The
    reasoner (``backend/cognitive/codb_reasoner.py``) filters these to what's
    affordable and ranks them by capability-gain-per-dollar. RECOMMEND-ONLY —
    nothing here is ever auto-purchased.
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    monthly_cost_usd: float = 0.0
    one_time_cost_usd: float = 0.0
    relieves_codb_id: str | None = None   # id in `costs` whose ceiling this lifts
    bottleneck: str = ""                   # kebab tag matched vs bottleneck_hint
    capability: str = ""                   # plain-English capability unlocked
    capability_gain: float = 1.0           # rough relative gain toward bottleneck
    current_state: str = ""                # the constraint being hit today

    def effective_cost_usd(self) -> float:
        """Cost charged against available headroom.

        Monthly options are charged at their monthly rate; a pure one-time
        option (monthly == 0) is charged at its one-time amount. When both are
        set, monthly dominates the affordability gate (the recurring commitment
        is the binding constraint) and the one-time is additive context.
        """
        if self.monthly_cost_usd > 0:
            return round(self.monthly_cost_usd, 2)
        return round(self.one_time_cost_usd, 2)


class CodbRegistry(BaseModel):
    """Parsed codb_registry.yaml. Validated on every load."""
    model_config = ConfigDict(extra="forbid")

    costs: list[CodbItem] = Field(default_factory=list)
    revenue_targets: CodbRevenueTargets = Field(default_factory=CodbRevenueTargets)
    codb_investments: list[CodbInvestmentOption] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Composite snapshot returned to callers
# ---------------------------------------------------------------------------

class CodbCategoryBurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    total_monthly_usd: float
    item_count: int


class RunwayCalc(BaseModel):
    """Days-of-runway derived from balance and total monthly burn."""
    model_config = ConfigDict(extra="forbid")

    available_balance_usd: float
    total_monthly_burn_usd: float
    daily_burn_usd: float
    days_of_runway: float | None  # None when burn is zero
    runway_alert_days: int
    alert_triggered: bool


class CodbSummary(BaseModel):
    """Aggregate view of the CODB registry — what we spend, by criticality."""
    model_config = ConfigDict(extra="forbid")

    total_monthly_burn_usd: float
    by_criticality: dict[str, float]   # criticality -> $ / month
    by_category: list[CodbCategoryBurn]
    cuttable_low_first: list[CodbItem]  # sorted asc by criticality then desc by $
    ts: str


# ---------------------------------------------------------------------------
# Liabilities registry shapes (liabilities.yaml)
# ---------------------------------------------------------------------------

LenderRelationship = Literal["family", "friend", "institution", "other"]


class Lender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    relationship: LenderRelationship
    notes: str = ""


class LoanEntry(BaseModel):
    """One tranche of borrowed money — append-only in liabilities.yaml."""
    model_config = ConfigDict(extra="forbid")

    lender_id: str
    date: _date
    amount_usd: float
    memo: str = ""


class RepaymentEntry(BaseModel):
    """One repayment toward a lender — append-only in liabilities.yaml."""
    model_config = ConfigDict(extra="forbid")

    lender_id: str
    date: _date
    amount_usd: float
    memo: str = ""


class LiabilitiesRegistry(BaseModel):
    """Parsed liabilities.yaml."""
    model_config = ConfigDict(extra="forbid")

    lenders: list[Lender] = Field(default_factory=list)
    loans: list[LoanEntry] = Field(default_factory=list)
    repayments: list[RepaymentEntry] = Field(default_factory=list)


class LenderBalance(BaseModel):
    """Per-lender derived view."""
    model_config = ConfigDict(extra="forbid")

    lender_id: str
    lender_name: str
    relationship: LenderRelationship
    loans_total_usd: float
    repayments_total_usd: float
    outstanding_usd: float
    loan_count: int
    repayment_count: int


class LiabilitiesSummary(BaseModel):
    """Aggregate view derived from the liabilities registry."""
    model_config = ConfigDict(extra="forbid")

    total_outstanding_usd: float
    total_loans_received_usd: float
    total_repayments_made_usd: float
    by_lender: list[LenderBalance]
    ts: str


# ---------------------------------------------------------------------------
# Declines registry shapes (declines.yaml)
# ---------------------------------------------------------------------------

DeclineSeverity = Literal["critical", "high", "medium", "low"]
CashDistressStatus = Literal["ok", "degraded", "critical"]


class DeclineEvent(BaseModel):
    """One failed-charge event."""
    model_config = ConfigDict(extra="forbid")

    date: _date
    vendor: str
    amount_usd: float | None = None
    funding_source: str = ""
    category: str = ""
    severity: DeclineSeverity = "medium"
    notes: str = ""


class DeclinesRegistry(BaseModel):
    """Parsed declines.yaml."""
    model_config = ConfigDict(extra="forbid")

    events: list[DeclineEvent] = Field(default_factory=list)


class DeclinesSummary(BaseModel):
    """Aggregate decline view + cash_distress evaluation."""
    model_config = ConfigDict(extra="forbid")

    window_days: int
    recent_total_count: int
    recent_by_severity: dict[str, int]
    cash_distress: CashDistressStatus
    distress_reasons: list[str]
    recent_events: list[DeclineEvent]
    ts: str


# ---------------------------------------------------------------------------
# Debt portfolio shapes (debts.yaml — gitignored)
# ---------------------------------------------------------------------------

DebtType = Literal[
    "mortgage", "court", "utility", "credit_card",
    "professional_services", "sewer", "other",
]
DebtTier = Literal[1, 2, 3]


class DebtContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_service_phone: str | None = None
    collections_phone: str | None = None
    mail_general: str | None = None
    mail_qwr: str | None = None
    web: str | None = None
    regulator: str | None = None


class ResolutionPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    recommended: bool = False
    cost_term: str = ""
    notes: str = ""


class KeyDate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    date: _date
    detail: str = ""


class Debt(BaseModel):
    """One structured creditor obligation. Phase 3 of the finance workcell."""
    model_config = ConfigDict(extra="forbid")

    id: str
    creditor: str
    type: DebtType
    status: str  # free-form so operator can be precise
    tier: DebtTier
    balance_usd: float | None = None
    balance_unknown: bool = False
    balance_note: str = ""
    account_number: str = ""
    account_holders: list[str] = Field(default_factory=list)
    secured_asset: str | None = None
    interest_rate_pct: float | None = None
    contact: DebtContact = Field(default_factory=DebtContact)
    resolution_paths: list[ResolutionPath] = Field(default_factory=list)
    key_dates: list[KeyDate] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    strategic_notes: str = ""
    supporting_documents: list[str] = Field(default_factory=list)


class DebtsRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    debts: list[Debt] = Field(default_factory=list)


class DebtTierTotal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: int
    debt_count: int
    confirmed_total_usd: float
    unknown_balance_count: int


class DebtPortfolioSummary(BaseModel):
    """Aggregate view across all debts."""
    model_config = ConfigDict(extra="forbid")

    debt_count: int
    confirmed_total_usd: float
    unknown_balance_count: int
    by_tier: list[DebtTierTotal]
    recommended_path_per_debt: dict[str, str]  # debt_id -> recommended path label
    ts: str
    registry_loaded: bool  # False when debts.yaml doesn't exist yet


# ---------------------------------------------------------------------------
# Action calendar shapes (actions.yaml — gitignored)
# ---------------------------------------------------------------------------

ActionStatus = Literal["open", "done", "deferred"]


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    debt_id: str | None = None
    when: str  # TODAY | THIS_WEEK | <ISO date string>
    due_date: _date
    action: str
    where: str = ""
    status: ActionStatus = "open"
    completed_date: _date | None = None
    completed_note: str = ""


class ActionsRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[ActionItem] = Field(default_factory=list)


class ActionsSummary(BaseModel):
    """Open-action buckets relative to a pinned 'today'."""
    model_config = ConfigDict(extra="forbid")

    open_total: int
    overdue_count: int
    due_today_count: int
    due_this_week_count: int
    overdue_actions: list[ActionItem]
    due_today_actions: list[ActionItem]
    due_this_week_actions: list[ActionItem]
    ts: str
    registry_loaded: bool


# ---------------------------------------------------------------------------
# Info-gap shapes (info_gaps.yaml — gitignored)
# ---------------------------------------------------------------------------

InfoGapPriority = Literal["critical", "high", "medium", "low"]
InfoGapStatus = Literal["open", "resolved"]


class InfoGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    priority: InfoGapPriority
    related_debt_id: str | None = None
    gap: str
    how_to_close: str
    status: InfoGapStatus = "open"
    resolved_date: _date | None = None
    resolution_note: str = ""


class InfoGapsRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gaps: list[InfoGap] = Field(default_factory=list)


class InfoGapsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_total: int
    open_by_priority: dict[str, int]
    critical_open: list[InfoGap]  # always surfaced separately
    ts: str
    registry_loaded: bool


# ---------------------------------------------------------------------------
# Hardship shapes (hardship.yaml — gitignored)
# ---------------------------------------------------------------------------

BankingVehicleStatus = Literal["not_activated", "active", "closed"]


class CalFreshContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = False
    case_number: str | None = None
    issuing_agency: str | None = None
    worker_id: str | None = None
    worker_phone: str | None = None
    notice_date: _date | None = None
    initial_benefits_usd: float | None = None
    recurring_benefits_usd: float | None = None
    recurring_through: _date | None = None
    household_size: int | None = None
    earned_income_monthly_usd: float | None = None
    adjusted_countable_income_monthly_usd: float | None = None
    net_countable_income_monthly_usd: float | None = None
    housing_expense_monthly_usd: float | None = None
    utility_expense_monthly_usd: float | None = None
    income_reporting_threshold_usd: float | None = None
    certification_period_start: _date | None = None
    certification_period_end: _date | None = None
    notes: str = ""


class BankingVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bank: str
    card_type: str = ""
    fdic_insured: bool = False
    status: BankingVehicleStatus = "not_activated"
    activate_url: str | None = None
    activate_phone: str | None = None
    routing_number: str | None = None
    account_number: str | None = None
    use_case: str = ""
    notes: str = ""


class HardshipEvidence(BaseModel):
    """Catch-all for additional hardship documents (SSI letters, unemployment, etc.)."""
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str = ""
    date: _date | None = None
    notes: str = ""


class HardshipContext(BaseModel):
    """Parsed hardship.yaml — full set of operator's hardship evidence."""
    model_config = ConfigDict(extra="forbid")

    calfresh: CalFreshContext = Field(default_factory=CalFreshContext)
    banking_vehicles: list[BankingVehicle] = Field(default_factory=list)
    other_evidence: list[HardshipEvidence] = Field(default_factory=list)
    registry_loaded: bool = False  # False when hardship.yaml doesn't exist


# ---------------------------------------------------------------------------
# Composite snapshot returned to callers
# ---------------------------------------------------------------------------

class FinanceSnapshot(BaseModel):
    """The big-picture snapshot — Stripe + CODB + Liabilities + Declines + Phase 3."""
    model_config = ConfigDict(extra="forbid")

    ts: str
    stripe_reachable: bool
    stripe_error: str | None = None
    balance: StripeBalance | None = None
    recent_charges_count: int = 0
    recent_charges_usd_total: float = 0.0
    recent_payouts_count: int = 0
    recent_payouts_usd_total: float = 0.0
    # Live recurring revenue (pulled from active Stripe subscriptions). Surfaces
    # MRR in the canonical snapshot so revenue that did NOT come through the
    # website/cash-engine webhook pipeline is still reflected automatically.
    active_subscriptions_count: int = 0
    mrr_usd: float = 0.0
    codb_summary: CodbSummary
    runway: RunwayCalc
    liabilities_summary: LiabilitiesSummary
    declines_summary: DeclinesSummary
    # Phase 3 additions:
    debt_portfolio: DebtPortfolioSummary
    actions_summary: ActionsSummary
    info_gaps_summary: InfoGapsSummary
    hardship_context: HardshipContext


# ---------------------------------------------------------------------------
# Request shapes (HTTP / SQS envelopes)
# ---------------------------------------------------------------------------

class SnapshotRequest(BaseModel):
    """`/snapshot` body — controls how much Stripe history to pull."""
    model_config = ConfigDict(extra="forbid")

    charges_limit: int = Field(default=10, ge=1, le=100)
    payouts_limit: int = Field(default=10, ge=1, le=100)


class CodbSummaryRequest(BaseModel):
    """`/codb_summary` body — no params today; reserved for filtering later."""
    model_config = ConfigDict(extra="forbid")


class RunwayRequest(BaseModel):
    """`/runway` body. Caller may override the live balance for what-if math."""
    model_config = ConfigDict(extra="forbid")

    override_balance_usd: float | None = None


class LiabilitiesRequest(BaseModel):
    """`/liabilities` body — no params today."""
    model_config = ConfigDict(extra="forbid")


class DeclinesRequest(BaseModel):
    """`/declines` body — caller may override the lookback window."""
    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(default=30, ge=1, le=365)


class DebtsRequest(BaseModel):
    """`/debts` body — no params today."""
    model_config = ConfigDict(extra="forbid")


class ActionsSummaryRequest(BaseModel):
    """`/actions` body — caller may pin a 'today' for testing / what-if."""
    model_config = ConfigDict(extra="forbid")

    today: _date | None = None
    week_window_days: int = Field(default=7, ge=1, le=30)


class InfoGapsRequest(BaseModel):
    """`/info_gaps` body — no params today."""
    model_config = ConfigDict(extra="forbid")


class HardshipRequest(BaseModel):
    """`/hardship` body — no params today."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Per-customer billing summary (used by the inbound-email handler so any
# reply lands with full context: paid? subscribed? what amount?)
# ---------------------------------------------------------------------------

CustomerBillingState = Literal[
    "unknown",       # email not present in Stripe at all
    "customer",      # has charges but no active subscription
    "subscriber",    # has at least one active subscription
    "lookup_failed", # Stripe call errored; state could not be determined
]


class CustomerChargeRow(BaseModel):
    """One charge row in the summary — normalized to dollars + ISO time."""
    model_config = ConfigDict(extra="forbid")

    charge_id: str
    amount_usd: float
    currency: str
    status: str
    paid: bool
    created_iso: str       # ISO-8601 UTC derived from Stripe's unix `created`
    description: str = ""


class CustomerSubscriptionRow(BaseModel):
    """One active-subscription row in the summary."""
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    status: str
    mrr_usd: float
    price_ids: list[str] = Field(default_factory=list)


class CustomerBillingSummary(BaseModel):
    """Per-customer financial snapshot.

    Returned by the finance ``customer_summary`` action; consumed by the
    inbound-email handler so a reply to outreach lands with the operator
    already knowing the customer's billing posture.

    ``state`` is the headline ("paid?", "subscribed?", "unknown"); the
    detailed rows let the handler render a richer task description without
    issuing a second lookup.
    """
    model_config = ConfigDict(extra="forbid")

    email: str
    state: CustomerBillingState
    stripe_customer_id: str = ""
    stripe_customer_ids: list[str] = Field(default_factory=list)  # if multiple
    livemode: bool = False
    total_paid_usd: float = 0.0      # sum of recent_charges
    mrr_usd: float = 0.0             # sum of active_subscriptions
    recent_charges: list[CustomerChargeRow] = Field(default_factory=list)
    active_subscriptions: list[CustomerSubscriptionRow] = Field(default_factory=list)
    lookup_error: str = ""           # populated on transport/auth failure
    ts: str                          # ISO-8601 UTC of this snapshot

    def one_line_summary(self) -> str:
        """Human-readable one-liner for embedding in a CRM task description.

        Examples:
          - "no Stripe customer for jane@x.com"
          - "Stripe customer cus_ABC: 2 charges totalling $890.00, no active sub"
          - "Stripe customer cus_ABC: $300/mo active sub (sub_XYZ), 3 prior charges $920.00"
        """
        if self.state == "lookup_failed":
            return f"billing lookup failed: {self.lookup_error}"
        if self.state == "unknown":
            return f"no Stripe customer for {self.email}"
        parts: list[str] = [f"Stripe customer {self.stripe_customer_id}"]
        if self.active_subscriptions:
            parts.append(f"${self.mrr_usd:,.2f}/mo active sub")
        else:
            parts.append("no active sub")
        if self.recent_charges:
            parts.append(
                f"{len(self.recent_charges)} charges totalling ${self.total_paid_usd:,.2f}",
            )
        return ", ".join(parts)
