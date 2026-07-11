"""Pydantic models for the voice workcell.

Two families:

  - Vapi REST shapes — only the fields the workcell reads. ``extra="ignore"``
    so Vapi can add fields without breaking deserialization.
  - Internal request/response shapes for the workcell's own HTTP surface.

The Morgan end-of-call structured payload (``recovery/vapi_sales_agent_config.md``
§Node 7) lives as :class:`LeadSummary` and is the only piece of the webhook
event the workcell strictly validates — everything else around it is treated
as a passthrough dict for the audit ledger.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Vapi REST shapes (only the fields the workcell reads)
# ---------------------------------------------------------------------------


class VapiCallCustomer(BaseModel):
    """The ``customer`` block on a Vapi call object."""

    model_config = ConfigDict(extra="ignore")

    number: str | None = None
    name: str | None = None


class VapiCall(BaseModel):
    """Subset of a Vapi /call response. Phase-1 fields only."""

    model_config = ConfigDict(extra="ignore")

    id: str
    status: str | None = None
    type: str | None = None
    assistantId: str | None = None
    phoneNumberId: str | None = None
    customer: VapiCallCustomer | None = None
    createdAt: str | None = None
    startedAt: str | None = None
    endedAt: str | None = None
    endedReason: str | None = None
    cost: float | None = None
    transcript: str | None = None
    recordingUrl: str | None = None
    summary: str | None = None

    @field_validator("customer", mode="before")
    @classmethod
    def _coerce_blank_customer(cls, v: Any) -> Any:
        """Tolerate Vapi returning ``customer`` as ``""`` instead of an object.

        Some Vapi ``GET /call`` rows carry ``customer: ""`` (empty string)
        rather than a customer object or ``null``. Without this, the row fails
        validation and ``VapiClient.list_calls`` silently drops it — thinning
        the console's recent-calls list. Any non-dict value coerces to None.
        """
        return v if isinstance(v, dict) else None


class VapiAssistant(BaseModel):
    """Subset of a Vapi /assistant response."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None


class VapiPhoneNumber(BaseModel):
    """Subset of a Vapi /phone-number response (the inbound receptionist DID)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    number: str | None = None
    name: str | None = None
    assistantId: str | None = None
    provider: str | None = None


# ---------------------------------------------------------------------------
# Webhook envelope (Vapi -> us)
# ---------------------------------------------------------------------------

WebhookMessageType = Literal[
    "end-of-call-report",
    "status-update",
    "transcript",
    "function-call",
    "hang",
    "speech-update",
    "conversation-update",
    "user-interrupted",
    "tool-calls",
]


class LeadSummary(BaseModel):
    """Structured ``lead_summary`` from Morgan's end-of-call JSON contract.

    Source: ``Samus/recovery/vapi_sales_agent_config.md`` Node 7. Fields are
    all optional because the agent may exit early (permission gate, gatekeeper
    block, low intent) and only fill a subset. ``intent_score`` is normalized
    to ``[0, 100]`` if the agent reports a value outside that range.

    ``recommended_action`` / ``prefers_text`` / ``contact_offered`` give the
    automated caller parity with the operator hand-call path: ``gatekeeper``
    is a first-class outcome (see :mod:`backend.crm.call_outcomes`), and a
    contact the agent is handed is recorded verbatim so the webhook can
    validate it via :mod:`backend.prospecting.contact_validation`.
    """

    model_config = ConfigDict(extra="ignore")

    company: str | None = None
    lead_volume: str | None = None
    automation_level: str | None = None
    pain_points: list[str] = Field(default_factory=list)
    intent_score: int | None = None
    tier: Literal["low", "medium", "high", "priority"] | None = None
    recommended_action: Literal["book_call", "follow_up", "disqualify", "gatekeeper"] | None = None
    # The prospect — or their voicemail greeting — asked to be reached by text
    # rather than a call. The automated analogue of the operator's
    # "[prefers_text]" capture in scripts/Log-Call.ps1.
    prefers_text: bool = False
    # An email / contact someone offered on the call for follow-up, recorded
    # VERBATIM — Morgan must not "correct" it, since a malformed address is
    # itself the misdirection signal. The end-of-call webhook runs
    # contact_validation.assess_email on it. Empty when none was offered.
    contact_offered: str | None = None

    # Product validation — populated when a specific offer resonated on the call.
    # Morgan sets these when the prospect positively engaged with a named product
    # and the conversation warrants adding/refreshing it on the public site.
    validated_product_name: str | None = None
    validated_product_description: str | None = None
    validated_product_price: str | None = None
    validated_product_deliverables: list[str] = Field(default_factory=list)


class VapiWebhookMessage(BaseModel):
    """The ``message`` block of a Vapi webhook payload."""

    model_config = ConfigDict(extra="ignore")

    type: str  # not Literal — Vapi adds new types regularly; unknown -> ignored
    call: dict[str, Any] | None = None
    transcript: str | None = None
    # Live (mid-call) transcript fields — used by the dormant mid-call
    # adaptation bridge (backend.voice.autonomous) to filter to a prospect's
    # FINAL turns. Vapi may omit them; optional + default None (extra=ignore
    # would otherwise drop them).
    role: str | None = None
    transcriptType: str | None = None
    summary: str | None = None
    endedReason: str | None = None
    recordingUrl: str | None = None
    # ``structuredData`` is where Vapi places the JSON Morgan emits at end-of-call.
    structuredData: dict[str, Any] | None = None
    # Call duration from Vapi's end-of-call-report. Metered receptionist
    # billing reports this to Stripe; callers fall back to
    # ``endedAt - startedAt`` off the ``call`` block when Vapi omits it.
    durationSeconds: float | None = None


class VapiWebhookEvent(BaseModel):
    """Top-level wrapper. Vapi nests everything under ``message``."""

    model_config = ConfigDict(extra="ignore")

    message: VapiWebhookMessage


# ---------------------------------------------------------------------------
# AI Digital Receptionist (inbound) — config + structured shapes
# ---------------------------------------------------------------------------


class InboundSummary(BaseModel):
    """Structured end-of-call output from the inbound receptionist assistant.

    Mirrors :class:`LeadSummary`: ``extra="ignore"`` and every field optional
    — the assistant may end a call early (caller hung up, wrong number) and
    fill only a subset. The receptionist assistant is configured to emit this
    JSON in ``structuredData`` at end-of-call.
    """

    model_config = ConfigDict(extra="ignore")

    caller_name: str | None = None
    reason_for_call: str | None = None
    appointment_requested: bool = False
    appointment_details: str | None = None  # free-text the operator books from
    callback_requested: bool = False
    callback_number: str | None = None
    message: str | None = None  # message left for the business
    urgent: bool = False  # caller flagged something urgent
    transferred: bool = False  # call was handed to a human


class BusinessHoursWindow(BaseModel):
    """Open/close window for one weekday. Both empty -> closed that day."""

    model_config = ConfigDict(extra="ignore")

    open: str = ""  # "HH:MM" 24-hour, business-local time
    close: str = ""

    @property
    def is_closed(self) -> bool:
        return not (self.open and self.close)


ReceptionistAfterHours = Literal["voicemail", "transfer", "message_only"]
ReceptionistSummaryCadence = Literal["weekly", "monthly"]
ReceptionistStatus = Literal["active", "paused"]


class ReceptionistConfig(BaseModel):
    """Per-client configuration for the AI Digital Receptionist.

    Pilot storage = a hand-edited YAML file at
    ``customers/<slug>/receptionist/config.yaml`` under SAMUS_ARTIFACT_ROOT.
    The loader (:mod:`backend.voice.receptionist_config`) is the swap point
    for a future DynamoDB-backed store; this model is the stable contract
    both the file loader and any future loader produce.
    """

    model_config = ConfigDict(extra="ignore")

    customer_slug: str  # PK — matches customers/<slug>/
    business_name: str = ""
    timezone: str = "America/Los_Angeles"  # IANA tz name
    phone_numbers: list[str] = Field(default_factory=list)  # E.164 DIDs -> this client
    greeting: str = ""  # spoken greeting line
    business_hours: dict[str, BusinessHoursWindow] = Field(default_factory=dict)
    after_hours_behavior: ReceptionistAfterHours = "voicemail"
    transfer_number: str = ""  # E.164 for urgent transfers
    voicemail_email: str = ""  # voicemail / escalation alerts
    summary_email: str = ""  # periodic call summary recipient
    summary_cadence: ReceptionistSummaryCadence = "weekly"
    vapi_assistant_id: str = ""
    vapi_phone_number_id: str = ""
    escalation_keywords: list[str] = Field(default_factory=list)
    booking_capture: bool = True  # capture appointment requests
    status: ReceptionistStatus = "active"
    # Stripe customer this client's metered call-minute usage bills to. Set
    # by the operator after creating the two-item subscription (Phase 7
    # runbook). Empty -> usage metering is skipped for this client (the
    # inbound call is still logged + persisted, just not billed).
    stripe_customer_id: str = ""
    # SKU the metered usage reports against — pins the Stripe Meter via the
    # retainer registry. Overridable per client; defaults to the receptionist.
    billing_sku_id: str = "retainer_ai_receptionist"
    # FIN-08 fail-closed gate. ``stripe_customer_id`` is read from an
    # operator-editable YAML, so a typo / stale / wrong id would otherwise be
    # forwarded into the finance ``/meter-event`` dispatch and bill ANOTHER
    # customer's meter. The config loader validates the id once at load time
    # (via an injected validator that asks finance — which holds the Stripe
    # secret — to confirm the customer exists) and, on an invalid / unknown /
    # validation-error result, sets ``metering_disabled=True`` with a reason.
    # The inbound caller's ``_report_usage`` then refuses to dispatch a meter
    # event (the call is still logged, just never billed to a wrong customer).
    # These are populated by the loader, not hand-edited in the YAML.
    metering_disabled: bool = False
    metering_disabled_reason: str = ""


class InboundCallRecord(BaseModel):
    """Normalized inbound call — persisted as ``calls/<call_id>/call.json``.

    The portal-facing shape: everything a future read-only client portal
    needs to render one call without touching CRM/DDB or re-querying Vapi.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str
    customer_slug: str
    caller_number: str = ""
    direction: Literal["inbound"] = "inbound"
    started_at: str = ""
    ended_at: str = ""
    duration_sec: int = 0
    answered: bool = False
    voicemail_left: bool = False
    ended_reason: str = ""
    summary: str = ""  # Vapi's end-of-call summary
    inbound_summary: InboundSummary = Field(default_factory=InboundSummary)
    recording_path: str = ""  # local path under calls/<id>/
    transcript_path: str = ""
    voicemail_path: str = ""
    written_at: str = ""  # iso timestamp at persist time


# ---------------------------------------------------------------------------
# Internal request / response shapes (workcell HTTP surface)
# ---------------------------------------------------------------------------


class InitiateCallRequest(BaseModel):
    """``POST /voice/call`` body. Either ``customer_number`` or full ``customer``."""

    model_config = ConfigDict(extra="forbid")

    assistant_id: str = Field(min_length=1)
    phone_number_id: str = Field(min_length=1)
    customer_number: str = Field(min_length=1)
    customer_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InitiateCallResult(BaseModel):
    """``POST /voice/call`` response."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    status: str | None = None
    vapi_error: str | None = None


class CallStatusResult(BaseModel):
    """``GET /voice/call/{id}`` response."""

    model_config = ConfigDict(extra="forbid")

    call: VapiCall | None = None
    vapi_error: str | None = None


class CallListResult(BaseModel):
    """``GET /voice/calls`` response."""

    model_config = ConfigDict(extra="forbid")

    calls: list[VapiCall] = Field(default_factory=list)
    vapi_error: str | None = None


class WebhookResult(BaseModel):
    """``POST /vapi/webhook`` response (status only; no body echo).

    Union note: HEAD added crm_dispatch_ok + crm_dispatch_error for the
    Phase-2 CRM Conversation + CallState persist path. origin had only the
    memory dispatch fields. Kept HEAD's stricter/fuller shape.
    """

    model_config = ConfigDict(extra="forbid")

    received: bool
    message_type: str
    call_id: str | None = None
    lead_summary: LeadSummary | None = None
    memory_dispatch_ok: bool = False
    memory_dispatch_error: str | None = None
    # Phase 2 — voice end-of-call also writes a Conversation row + refreshes
    # the per-prospect CallState FSM in samus-crm. Best-effort: a CRM-side
    # failure does NOT fail the webhook (memory is the primary persistence).
    crm_dispatch_ok: bool = False
    crm_dispatch_error: str | None = None


# ---------------------------------------------------------------------------
# Morning dialer shapes
# ---------------------------------------------------------------------------


class DialerConfig(BaseModel):
    """Per-run dialer configuration. Operator-tunable; defaults are cautious."""

    model_config = ConfigDict(extra="forbid")

    # Maximum prospects to dial in a single run. Hard cap on blast radius —
    # bad scripts / voicemail loops can't drain Vapi credit unattended.
    max_calls: int = Field(default=5, ge=1, le=50)
    # Wall-clock seconds between successive dial attempts. Vapi has its own
    # concurrency limits; this is operator-side pacing so a stuck call doesn't
    # stack three more before the operator notices.
    delay_between_calls_s: float = Field(default=30.0, ge=0.0, le=600.0)
    # When True, no /call is sent — the dialer just reports who it WOULD call
    # and why. Default True so operator approval is the explicit step.
    dry_run: bool = True
    # Eligible priority buckets in dial order. "low" is excluded by default
    # so the morning push targets qualified leads only.
    only_priorities: list[Literal["hot", "warm", "low"]] = Field(
        default_factory=lambda: ["hot", "warm"],
    )
    # TCPA call-hours gate. Hard refuse outside 8am-9pm prospect-local. Set
    # both to 0 to disable the gate (operator owns TCPA risk in that mode).
    call_hours_start: int = Field(default=8, ge=0, le=23)
    call_hours_end: int = Field(default=21, ge=1, le=24)
    # When True and at least 5 calls were initiated, run tune_assistant in
    # dry_run=False mode after the dial session completes. Failures are
    # non-fatal (logged as WARNING). Default False — operator opt-in.
    auto_tune: bool = False
    # Round-robin phone number rotation. When non-empty, the dialer cycles
    # through these phone_number_ids instead of the single VAPI_PHONE_NUMBER_ID
    # env var, distributing call volume across numbers to slow reputation decay.
    # Empty list = use settings.vapi_phone_number_id (single-number mode).
    phone_number_ids: list[str] = Field(default_factory=list)


class DialAttempt(BaseModel):
    """One row in the per-run audit. Captures the decision + the API outcome."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    company: str
    phone: str
    state: str
    priority: str
    # outcome: 'initiated' (call_id present), 'skipped_hours' (TCPA gate),
    # 'skipped_priority' (filtered out by only_priorities), 'skipped_phone'
    # (no/invalid phone), 'dry_run' (would have dialed), 'vapi_error' (Vapi
    # REST failure — vapi_error populated).
    outcome: Literal[
        "initiated",
        "skipped_hours",
        "skipped_priority",
        "skipped_phone",
        "skipped_suppressed",
        "skipped_already_called",
        "skipped_cooldown",
        "dry_run",
        "vapi_error",
    ]
    call_id: str | None = None
    vapi_error: str | None = None
    detail: str = ""
    initiated_at: str  # iso_now()
    outbound_number_id: str = ""


class DialRunResult(BaseModel):
    """Aggregate of one dialer pass over a CSV. Persisted as JSONL row."""

    model_config = ConfigDict(extra="forbid")

    run_id: str  # e.g. "dial_run_2026-05-15T14:32:08Z"
    ts: str
    csv_path: str
    config: DialerConfig
    eligible_count: int  # priority-filtered, before max_calls cap + TCPA gate
    initiated_count: int
    skipped_count: int
    error_count: int
    attempts: list[DialAttempt]


class DialCallListRequest(BaseModel):
    """``POST /voice/dial_call_list`` body."""

    model_config = ConfigDict(extra="forbid")

    csv_path: str | None = None  # default: today's call_list_YYYY-MM-DD.csv
    config: DialerConfig = Field(default_factory=DialerConfig)


# ---------------------------------------------------------------------------
# Morning-briefing rollup shapes
# ---------------------------------------------------------------------------


class VoiceCallSummaryRow(BaseModel):
    """One end-of-call row surfaced in the briefing's voice block."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    received_at: str
    company: str = ""
    tier: str = ""  # "" if Morgan didn't score
    intent_score: int | None = None
    recommended_action: str = ""


class VoiceCallSummary(BaseModel):
    """Rollup of voice_audit.jsonl over the last ``window_hours``."""

    model_config = ConfigDict(extra="forbid")

    window_hours: int
    total_calls: int
    initiated_count: int  # action='initiate_call' in window
    end_of_call_count: int  # action='webhook_end_of_call' in window
    # Dialer activity — separate from initiate_count because dial_attempt rows
    # include dry-runs + skips + errors, not just live Vapi calls placed.
    # The breakdown lets the briefing surface "I tried N, dialed M, skipped K".
    dial_attempt_count: int = 0
    dial_attempts_by_outcome: dict[str, int] = Field(default_factory=dict)
    by_recommended_action: dict[str, int]  # book_call / follow_up / disqualify / <empty>
    by_tier: dict[str, int]  # priority / high / medium / low / <empty>
    avg_intent_score: float | None
    booked_calls: list[VoiceCallSummaryRow]  # recommended_action='book_call'
    recent_high_intent: list[VoiceCallSummaryRow]  # tier in ('high', 'priority')
    answer_rate_by_number: dict[str, float] = Field(default_factory=dict)
    dnc_count: int = 0
    retry_queue_depth: int = 0
    log_loaded: bool
    ts: str


# ---------------------------------------------------------------------------
# Post-batch voice tuner shapes
# ---------------------------------------------------------------------------


class TuneChange(BaseModel):
    field: str
    reason: str
    old_value: str
    new_value: str
    applied: bool = False
    rejected_reason: str | None = None


class TuneResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_id: str
    ts: str
    calls_analyzed: int
    observations: list[str]
    changes_proposed: list[TuneChange]
    changes_applied: int
    changes_rejected: int
    dry_run: bool
    skip_reason: str | None = None
    llm_error: str | None = None
    vapi_error: str | None = None
