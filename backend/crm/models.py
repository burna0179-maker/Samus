"""Pydantic models for the CRM workcell — 7 entity types + feedback engine I/O.

Prospect reuses :class:`backend.prospecting.models.ProspectRecord` because it
already has the 32-field shape the call-list workflow depends on. The other
6 entities are new and modeled fresh against the actual DDB table schemas
(which evolved from the recovery/prospect_schema.py spec: accounts folded
into prospects, activities split into conversations + call-State, task PK
renamed to operator_task_id).

The :class:`FeedbackLogRequest` + :class:`FeedbackSnapshotResponse` at the
bottom of this file are the I/O contracts for the in-memory feedback engine
in :mod:`backend.crm.feedback_engine`.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.common.stake_sentence_guard import (
    StakeSentenceRejected,
    validate_stake_sentence,
)
from backend.prospecting.models import ProspectRecord


def _validate_stake_sentence_field(value: str) -> str:
    text = (value or "")
    if not text.strip():
        return ""
    try:
        validate_stake_sentence(text)
    except StakeSentenceRejected as exc:
        raise ValueError(str(exc)) from exc
    return text


class Prospect(ProspectRecord):
    """CRM-side Prospect model.

    Subclasses :class:`backend.prospecting.models.ProspectRecord` to inherit
    its 32-field call-list shape, but flips ``extra="ignore"`` so reads of
    legacy DDB rows (which carry pre-iteration fields like ``account_id``
    and ``campaign_name``) don't get silently dropped by validation. Writes
    from this workcell still produce clean, prospecting-compatible items
    because we only set the documented constructor args.

    The strict ``extra="forbid"`` on ProspectRecord stays in place for the
    CSV-export path in :mod:`backend.prospecting.csv_export`, which depends
    on the field set being closed.
    """
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Contact — a person at a Prospect (company)
# ---------------------------------------------------------------------------

ContactChannel = Literal["email", "phone", "sms", "linkedin"]


class Contact(BaseModel):
    """One person associated with a Prospect. PK=contact_id."""
    model_config = ConfigDict(extra="ignore")

    contact_id: str
    prospect_id: str = ""               # FK -> samus_prospects.prospect_id
    name: str = ""
    role: str = ""                      # CEO, owner, marketing manager, etc.
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    preferred_channel: ContactChannel = "email"
    do_not_contact: bool = False
    source: str = ""                    # "intake_lead", "manual", "import", etc.
    source_ref: str = ""                # source entity id (e.g., lead_id)
    created_at: str = ""                # ISO-8601
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Conversation — a multi-turn exchange thread (call transcript, email chain)
# ---------------------------------------------------------------------------

ConversationChannel = Literal["call", "email", "sms", "linkedin", "in_person"]
ConversationStatus = Literal["open", "completed", "abandoned"]
# Outbound = Samus dialed out (the sales dialer). Inbound = a caller rang a
# receptionist client's number. Defaults to "outbound" so every row written
# before the receptionist shipped reads back unchanged.
ConversationDirection = Literal["inbound", "outbound"]


class Conversation(BaseModel):
    """A multi-turn exchange thread. PK=conversation_id."""
    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    prospect_id: str = ""
    contact_id: str = ""
    channel: ConversationChannel = "call"
    status: ConversationStatus = "open"
    started_at: str = ""                # ISO-8601
    ended_at: str = ""
    duration_sec: int = 0
    transcript: str = ""                # full transcript text (long string)
    summary: str = ""                   # LLM-generated synopsis
    structured_data: dict[str, Any] = Field(default_factory=dict)  # e.g., lead_summary
    outcome: str = ""                   # "booked", "follow_up", "disqualified", etc.
    recording_url: str = ""             # external storage URL if any
    source: str = ""                    # "vapi", "operator", "import"
    source_ref: str = ""                # e.g., Vapi call id
    # --- inbound receptionist fields (default to the outbound-call shape so
    # existing rows are non-breaking; model_config is extra="ignore") -------
    direction: ConversationDirection = "outbound"
    customer_id: str = ""               # receptionist CLIENT slug (the
                                        # business whose phone was answered) —
                                        # NOT the caller; "" for outbound rows
    caller_number: str = ""             # inbound caller ID (E.164)
    answered: bool = False              # True if the receptionist picked up
    voicemail_left: bool = False        # True if the caller left a message


# ---------------------------------------------------------------------------
# CallState — current FSM state of the outbound dialer per prospect
# Note the table name is "samus_call-State" with the hyphen + capital S.
# PK is prospect_id (one current state per prospect, not per attempt).
# ---------------------------------------------------------------------------

CallStateValue = Literal[
    "not_called",
    "outreach_sent",   # an outreach message (e.g. a cold email) has gone out;
                       # the prospect is now awaiting a follow-up call — the
                       # scheduled day is in CallState.next_attempt_at
    "queued",
    "dialing",
    "in_progress",
    "completed",
    "gatekeeper",      # reached a gatekeeper, not the decision-maker — the
                       # call did NOT conclude, so this is a non-terminal
                       # state: the prospect stays callable (retry, now with
                       # the DM name/contact captured in notes / last_outcome)
    "no_answer",
    "voicemail",
    "busy",
    "failed",
    "do_not_call",
]


class CallState(BaseModel):
    """Current call-attempt state per prospect. PK=prospect_id."""
    model_config = ConfigDict(extra="ignore")

    prospect_id: str
    state: CallStateValue = "not_called"
    attempt_count: int = 0
    last_attempt_at: str = ""
    next_attempt_at: str = ""           # for retries / scheduled re-dial
    last_call_id: str = ""              # most recent Vapi call_id
    last_outcome: str = ""              # human-readable last outcome
    notes: str = ""
    updated_at: str = ""


# --- mass-assignment hardening (finding M3, 2026-05-20) --------------------
# Path-id-free input shape for ``POST /crm/call-state/{prospect_id}``. The
# route binds the body to this, then constructs the full :class:`CallState`
# with ``prospect_id`` forced from the URL path. This route IS the canonical
# dialer-FSM writer (there is no separate per-field transition action), so
# every state field stays caller-settable — but ``extra="forbid"`` now
# rejects any unknown key instead of silently dropping it, and the path id
# can no longer be overridden by a body value.

class UpsertCallStateBody(BaseModel):
    """``POST /crm/call-state/{prospect_id}`` request body (no path id)."""
    model_config = ConfigDict(extra="forbid")

    state: CallStateValue = "not_called"
    attempt_count: int = 0
    last_attempt_at: str = ""
    next_attempt_at: str = ""
    last_call_id: str = ""
    last_outcome: str = ""
    notes: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# FollowUpDue — a derived view (NOT a stored table): a prospect whose outreach
# follow-up call is due. service.list_follow_ups_due builds it by joining a
# CallState in the "outreach_sent" state (the due signal) with the originating
# outreach Conversation, which carries the company name + phone the operator
# needs to place the call — the outreach workcell only knew the prospect by
# id + email, so that contact detail has to come from the Conversation.
# ---------------------------------------------------------------------------

class FollowUpDue(BaseModel):
    """One prospect whose outreach follow-up call is due today or earlier."""
    model_config = ConfigDict(extra="ignore")

    prospect_id: str
    company: str = ""
    phone: str = ""
    channel: str = ""               # outreach channel used (email / sms)
    subject: str = ""               # the outreach message subject line
    emailed_on: str = ""            # date the outreach went out (YYYY-MM-DD)
    follow_up_on: str = ""          # scheduled follow-up day (YYYY-MM-DD)
    days_waiting: int = 0           # whole days since the outreach went out
    attempt_count: int = 0          # CallState.attempt_count
    # suggested "second opportunity" — a deterministic upsell pick from the
    # catalog (backend.catalog.registry), keyed off interest signals in the
    # prospect's conversations + Opportunity. All three stay "" when no signal
    # cleared the bar — the morning brief then shows no upsell hint.
    upsell_sku: str = ""            # catalog sku_id, e.g. "service_workflow_buildout"
    upsell_name: str = ""           # catalog display_name
    upsell_pitch: str = ""          # one-line operator hint — what to offer + why


class FollowUpList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    follow_ups: list[FollowUpDue] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


# ---------------------------------------------------------------------------
# Opportunity — a sales deal with pipeline stage
# ---------------------------------------------------------------------------

OpportunityStage = Literal[
    "new",
    "qualified",
    "proposal",
    "negotiation",
    "closed_won",
    "closed_won_retainer",
    "closed_lost",
]


class Opportunity(BaseModel):
    """One sales deal. PK=opportunity_id."""
    model_config = ConfigDict(extra="ignore")

    opportunity_id: str
    prospect_id: str = ""
    contact_id: str = ""
    stage: OpportunityStage = "new"
    name: str = ""                      # human-friendly deal name
    deal_size_usd: float = 0.0
    close_probability: float = 0.0      # 0.0 - 1.0
    next_step: str = ""
    expected_close: str = ""            # ISO-8601 date
    actual_close: str = ""              # set on closed_won / closed_lost
    won_amount_usd: float = 0.0         # populated on closed_won
    lost_reason: str = ""               # populated on closed_lost
    assigned_to: str = ""               # operator email
    created_at: str = ""
    updated_at: str = ""
    # --- strategy bandit attribution (strategy-integration build, Unit 3) ---
    # Captured at Opportunity creation so the deal outcome can be credited to
    # the exact bandit arm (`industry::policy_family`) that picked the prospect
    # and scored by reward density. ``industry`` + ``policy_family`` are the
    # composite hierarchical-bandit arm; the four signals below are a snapshot
    # of the prospect's REAL backend.strategy.reward_density.RewardSignal
    # inputs taken at creation time (the enrichment that was true when the deal
    # opened — not whatever the prospect row says months later). All default
    # so a pre-this-feature Opportunity reads back unchanged (no bandit credit).
    industry: str = ""
    policy_family: str = ""
    seo_score: int = 0                  # 0-100 audit score (normalised later)
    owner_email: bool = False           # enrichment found an owner email
    social_facebook: bool = False       # enrichment found a Facebook handle
    social_instagram: bool = False      # enrichment found an Instagram handle
    # Total LLM dollars spent on this prospect during discovery (strategy-
    # integration build, Unit 4): the personalised callsheet + the warm/hot
    # audit's content drafts, priced from the Anthropic usage block. Fed into
    # RewardSignal.token_cost_usd so the reward-density model can tell a cheap
    # win from an expensive one. 0.0 on a pre-this-feature Opportunity.
    token_cost_usd: float = 0.0
    # --- stake_sentence (operator-authored line; outreach refuses without it) -
    # Defaults empty so existing creation paths (lead conversion, log_call
    # booking) can still mint an Opportunity in the "no stake yet" state. The
    # outreach + report + callsheet wire-ups refuse to fire while it is empty.
    # Non-empty values are guard-validated (length window + banned phrases +
    # casing + ASCII ratio) — see :mod:`backend.common.stake_sentence_guard`.
    stake_sentence: str = ""
    stake_sentence_authored_by: str = ""
    stake_sentence_authored_at: str = ""

    @field_validator("stake_sentence")
    @classmethod
    def _check_stake_sentence(cls, value: str) -> str:
        return _validate_stake_sentence_field(value)


# ---------------------------------------------------------------------------
# OperatorTask — human-facing to-do item
# Note the PK is "operator_task_id", not "task_id" — DDB schema.
# ---------------------------------------------------------------------------

TaskStatus = Literal["open", "in_progress", "done", "skipped"]
TaskKind = Literal[
    "call",
    "review",
    "sign",
    "deliver",
    "follow_up",
    "send_email",
    "send_proposal",
    "schedule",
    "reply_email",       # inbound customer email surfaced by gmail poller
    "client_correspondence",  # inbound email from a signed/existing client
    "customer_service",  # signed-client message flagged as service issue / escalation
    "other",
]


class OperatorTask(BaseModel):
    """A human-actionable to-do. PK=operator_task_id."""
    model_config = ConfigDict(extra="ignore")

    operator_task_id: str
    kind: TaskKind = "other"
    title: str = ""                     # short label shown in lists
    description: str = ""                # longer body
    assignee: str = ""                  # operator email
    status: TaskStatus = "open"
    due_at: str = ""                    # ISO-8601
    completed_at: str = ""
    related_entity_kind: str = ""       # "prospect", "contact", "opportunity", "lead"
    related_entity_id: str = ""         # the ref id
    source: str = ""                    # what produced this task
    source_ref: str = ""
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Artifact — a generated deliverable (proposal, SEO report, callsheet, etc.)
# ---------------------------------------------------------------------------

ArtifactKind = Literal[
    "call_sheet",
    "seo_audit",
    "seo_report",
    "proposal",
    "content_draft",
    "fulfillment_plan",
    "contract",
    "invoice",
    "inbound_email",     # raw parsed inbound message stored by gmail poller
    "client_correspondence",  # inbound email from a signed/existing client
    "calendar_event",    # samus- or operator-created event on the polled calendar
    "call_recording",    # inbound-call audio captured by the receptionist
    "call_transcript",   # inbound-call transcript text
    "voicemail",         # after-hours / unanswered-call message
    "stake_sentence",    # operator-authored opening line attached to an Opp
    "other",
]


class Artifact(BaseModel):
    """A generated deliverable. PK=artifact_id."""
    model_config = ConfigDict(extra="ignore")

    artifact_id: str
    kind: ArtifactKind = "other"
    owner_entity_kind: str = ""         # "prospect" | "opportunity" | "contact"
    owner_entity_id: str = ""           # the ref id
    title: str = ""
    storage_url: str = ""               # S3/GCS path if file is offsite
    inline_data: dict[str, Any] = Field(default_factory=dict)  # small artifacts in-row
    mime_type: str = ""
    bytes: int = 0
    source: str = ""                    # workcell that produced it
    created_at: str = ""
    created_by: str = ""                # operator email or workcell name


# ---------------------------------------------------------------------------
# Conversion request/result (POST /crm/convert/lead)
# ---------------------------------------------------------------------------

class ConvertLeadRequest(BaseModel):
    """``POST /crm/convert/lead`` body."""
    model_config = ConfigDict(extra="forbid")

    lead_id: str = Field(min_length=1)
    assigned_to: str = ""               # operator email to assign follow-up tasks


class ConvertLeadResult(BaseModel):
    """``POST /crm/convert/lead`` response."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "existing", "failed"]
    prospect_id: str
    contact_id: str
    lead_id: str
    ts: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Upsert request shapes / shared result
# (POST /crm/conversations, POST /crm/call-state/{prospect_id})
# ---------------------------------------------------------------------------

class UpsertResult(BaseModel):
    """Generic upsert response — Conversation + CallState writers share this.

    ``persisted`` is the DDB-put outcome (False on AWS/IAM hiccup; caller can
    inspect ``error`` for the underlying ddb_put_failed string). ``id`` is the
    PK of the row that was upserted so callers can correlate retries.
    """
    model_config = ConfigDict(extra="forbid")

    persisted: bool
    id: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Opportunity create + advance (Phase 3)
# ---------------------------------------------------------------------------

class CreateOpportunityRequest(BaseModel):
    """``POST /crm/opportunities`` body.

    ``prospect_id`` is the only hard requirement. ``intent_score`` /
    ``monthly_budget`` / ``service_interest`` are optional — if provided
    the service layer auto-fills tier / close_probability / deal_size via
    :mod:`backend.crm.scoring`.
    """
    model_config = ConfigDict(extra="forbid")

    prospect_id: str = Field(min_length=1)
    contact_id: str = ""
    name: str = ""                      # human-friendly deal name
    intent_score: int | None = None     # 0-100 (from lead summarizer)
    monthly_budget: str = ""            # intake enum: e.g. "$500-$2000"
    service_interest: list[str] = Field(default_factory=list)
    assigned_to: str = ""               # operator email
    next_step: str = ""
    expected_close: str = ""            # ISO-8601 date
    # --- strategy bandit attribution (strategy-integration build, Unit 3) ---
    # Captured at Opportunity creation so the deal outcome can be credited to
    # the exact bandit arm + scored by reward density. ``industry`` +
    # ``policy_family`` are the composite hierarchical-bandit arm; the four
    # signals below are a snapshot of the prospect's REAL RewardSignal inputs
    # (see backend.strategy.reward_density.RewardSignal). All optional —
    # absent on an operator-created deal that has no call-list provenance.
    industry: str = ""
    policy_family: str = ""
    seo_score: int = 0                  # 0-100 audit score
    owner_email: bool = False           # enrichment found an owner email
    social_facebook: bool = False       # enrichment found a Facebook handle
    social_instagram: bool = False      # enrichment found an Instagram handle
    # Per-prospect LLM dollars spent during discovery (strategy-integration
    # build, Unit 4) — see Opportunity.token_cost_usd. Optional — absent on an
    # operator-created deal that has no call-list provenance.
    token_cost_usd: float = 0.0
    # Operator-authored stake_sentence. Optional at create time because the
    # auto-creation paths (lead conversion, booked-call hand-off) have no
    # operator-typed line yet; the canonical authoring path is the dedicated
    # stake CLI / console route, which runs the budget + guard + dedup +
    # artifact pipeline. When supplied at create time it is guard-validated
    # immediately (banned phrases + length + casing + ASCII ratio).
    stake_sentence: str = ""
    stake_sentence_authored_by: str = ""

    @field_validator("stake_sentence")
    @classmethod
    def _check_stake_sentence(cls, value: str) -> str:
        return _validate_stake_sentence_field(value)


class CreateOpportunityResult(BaseModel):
    """``POST /crm/opportunities`` response."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "failed"]
    opportunity_id: str
    ts: str
    error: str | None = None


class AdvanceOpportunityRequest(BaseModel):
    """``POST /crm/opportunities/{opportunity_id}/advance`` body.

    ``opportunity_id`` is supplied in the URL path AND validated against
    the body so the worker contract (which only sees the body) stays
    self-contained.
    """
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str = Field(min_length=1)
    target_stage: Literal[
        "new", "qualified", "proposal", "negotiation",
        "closed_won", "closed_won_retainer", "closed_lost",
    ]
    lost_reason: str = ""               # required if target == closed_lost
    won_amount_usd: float = 0.0         # required if target == closed_won
    notes: str = ""


# --- mass-assignment hardening (finding M3, 2026-05-20) --------------------
# The HTTP route for an opportunity advance binds the request body to this
# narrow model — which has NO ``opportunity_id`` field. The route then
# constructs the full :class:`AdvanceOpportunityRequest` server-side, forcing
# ``opportunity_id`` from the URL path. A caller therefore cannot smuggle a
# different opportunity_id through the body, and ``extra="forbid"`` rejects any
# unknown key rather than silently folding it onto the domain model. The
# remaining fields (``target_stage`` / ``won_amount_usd`` / ``lost_reason`` /
# ``notes``) ARE the legitimate inputs of the advance transition — the FSM in
# ``service.advance_opportunity`` governs whether a given target_stage is a
# valid move, so these stay caller-settable by design.

class AdvanceOpportunityBody(BaseModel):
    """``POST /crm/opportunities/{opportunity_id}/advance`` request body.

    Path-id-free input shape — the route supplies ``opportunity_id`` from the
    URL. See the M3 note above.
    """
    model_config = ConfigDict(extra="forbid")

    target_stage: Literal[
        "new", "qualified", "proposal", "negotiation",
        "closed_won", "closed_won_retainer", "closed_lost",
    ]
    lost_reason: str = ""               # required if target == closed_lost
    won_amount_usd: float = 0.0         # required if target == closed_won
    notes: str = ""


class AdvanceOpportunityResult(BaseModel):
    """``POST /crm/opportunities/{opportunity_id}/advance`` response."""
    model_config = ConfigDict(extra="forbid")

    # ``blocked_over_cap`` (redteam FIN-10): the advance was refused because the
    # requested won_amount_usd exceeded SAMUS_CRM_MAX_CLOSE_AMOUNT_USD. The
    # opportunity was NOT advanced; ``new_stage`` echoes the unchanged current
    # stage and ``error`` carries the structured "closed_won_blocked_over_cap"
    # reason. A structured blocked result (not an exception) so the Stripe
    # webhook + SQS worker callers — which only read ``status`` and never catch
    # — surface it gracefully.
    status: Literal[
        "advanced", "invalid_transition", "not_found", "failed",
        "blocked_over_cap",
    ]
    opportunity_id: str
    prior_stage: str = ""
    new_stage: str = ""
    ts: str
    error: str | None = None
    # Telemetry (additive, 2026-05-20) — whole seconds from the Opportunity's
    # creation to its terminal close. Set only on a successful advance into a
    # terminal stage (closed_won / closed_won_retainer / closed_lost); stays
    # None on a non-terminal advance or when the timestamps are unparseable.
    latency_to_resolution_sec: float | None = None


# ---------------------------------------------------------------------------
# Operator task create + update (Phase 4)
# ---------------------------------------------------------------------------

class CreateOperatorTaskRequest(BaseModel):
    """``POST /crm/operator-tasks`` body.

    ``kind`` + ``title`` are the only hard requirements; everything else is
    optional metadata that the lifecycle auto-generators populate when they
    have it. ``related_entity_kind`` / ``related_entity_id`` are paired —
    the service layer doesn't enforce the pairing (one without the other is
    still valid for ad-hoc operator-created tasks), but the auto-generators
    always set both.
    """
    model_config = ConfigDict(extra="forbid")

    kind: TaskKind
    title: str = Field(min_length=1)
    description: str = ""
    assignee: str = ""                  # operator email
    due_at: str = ""                    # ISO-8601
    related_entity_kind: str = ""       # "prospect", "contact", "opportunity", "lead"
    related_entity_id: str = ""
    source: str = ""                    # what produced this task
    source_ref: str = ""


class CreateOperatorTaskResult(BaseModel):
    """``POST /crm/operator-tasks`` response."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "failed"]
    operator_task_id: str
    ts: str
    error: str | None = None


class UpdateOperatorTaskRequest(BaseModel):
    """``PUT /crm/operator-tasks/{operator_task_id}`` body.

    Service layer validates the status transition (open -> in_progress ->
    done/skipped; can't go backwards). ``completed_at`` defaults to the
    server timestamp when the new status is terminal (done/skipped) and
    the caller didn't supply one.
    """
    model_config = ConfigDict(extra="forbid")

    operator_task_id: str = Field(min_length=1)
    status: TaskStatus
    completed_at: str = ""              # ISO-8601 (auto-set on done/skipped)
    notes: str = ""                     # appended to description if non-empty


# --- mass-assignment hardening (finding M3, 2026-05-20) --------------------
# Path-id-free input shape for the operator-task update route. The route binds
# the body to this, then builds the full :class:`UpdateOperatorTaskRequest`
# with ``operator_task_id`` forced from the URL path. ``status`` IS the
# legitimate input of the update transition — the service layer validates the
# status move (open -> in_progress -> done/skipped, no going backwards) — so
# it stays caller-settable. ``completed_at`` is kept caller-settable because
# the existing contract allows an explicit terminal timestamp (the service
# auto-fills it only when omitted); ``notes`` is appended to the description.

class UpdateOperatorTaskBody(BaseModel):
    """``PUT /crm/operator-tasks/{operator_task_id}`` request body.

    Path-id-free input shape — the route supplies ``operator_task_id`` from
    the URL. See the M3 note above.
    """
    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    completed_at: str = ""              # ISO-8601 (auto-set on done/skipped)
    notes: str = ""                     # appended to description if non-empty


class UpdateOperatorTaskResult(BaseModel):
    """``PUT /crm/operator-tasks/{operator_task_id}`` response."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["updated", "invalid_transition", "not_found", "failed"]
    operator_task_id: str
    prior_status: str = ""
    new_status: str = ""
    ts: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Artifact create (Phase 5 — deliverable registration)
# ---------------------------------------------------------------------------

class CreateArtifactRequest(BaseModel):
    """``POST /crm/artifacts`` body.

    Producer workcells (seo, proposal, fulfillment, contract) register
    a generated deliverable so the operator can later pull "all
    deliverables for opportunity X" from CRM. The ``inline_data`` field is
    for small payloads that live in the DDB row itself; ``storage_url`` is
    the offsite pointer (S3/GCS/local-fs path) for anything substantial.
    """
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    owner_entity_kind: str = Field(min_length=1)  # "prospect"|"opportunity"|"contact"
    owner_entity_id: str = Field(min_length=1)
    title: str = ""
    storage_url: str = ""
    inline_data: dict[str, Any] = Field(default_factory=dict)
    mime_type: str = ""
    bytes: int = 0
    source: str = ""                    # workcell that produced it
    created_by: str = ""                # operator email or workcell name


class CreateArtifactResult(BaseModel):
    """``POST /crm/artifacts`` response."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "failed"]
    artifact_id: str
    ts: str
    error: str | None = None


# ---------------------------------------------------------------------------
# List-result wrappers (used by ?param= scan endpoints)
# ---------------------------------------------------------------------------

class ContactList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contacts: list[Contact] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


class ConversationList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversations: list[Conversation] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


class OpportunityList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunities: list[Opportunity] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


class OperatorTaskList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[OperatorTask] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


class ArtifactList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[Artifact] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


# ---------------------------------------------------------------------------
# Feedback engine I/O (Phase 6 — in-memory METRICS v1)
# ---------------------------------------------------------------------------


class FeedbackLogRequest(BaseModel):
    """Payload for the ``log_feedback`` action — records a call outcome.

    ``outcome == "closed"`` increments product close counts + angle wins;
    anything else increments product failures + angle losses. ``objection``
    is optional — when present, increments the per-category objection
    counter regardless of the close/failure path.
    """

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    outcome: str = Field(..., description="'closed' or any other string")
    objection: str | None = None
    product: str
    angle: str


class FeedbackSnapshotResponse(BaseModel):
    """Response shape for ``get_feedback_snapshot`` — current METRICS state."""

    model_config = ConfigDict(extra="forbid")

    objections: dict[str, int] = Field(default_factory=dict)
    closes: dict[str, int] = Field(default_factory=dict)
    failures: dict[str, int] = Field(default_factory=dict)
    angles: dict[str, dict[str, Any]] = Field(default_factory=dict)
