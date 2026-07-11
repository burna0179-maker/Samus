"""I/O contracts for the Cash Engine front door.

``RevenueTriggerRequest`` is the *raw intent* a caller presents at
``POST /api/samus/review_opportunity``. It deliberately does NOT carry the
Stake Sentence: stake is operator-authored truth that lives on the
Opportunity row, and the gate reads it server-side from CRM. Letting a
caller assert "this prospect is staked" in the payload would be a trust-
boundary hole — the whole point of the front door is that the human's
signature, not the caller's claim, is the unlock.

``RevenueTriggerResult`` is the structured verdict the door returns. On a
blocked gate it names the failing protocol (``required_protocol`` for a
missing precondition like the Stake Sentence, ``violated_rule_id`` for a
Codex rule) so the caller — and the operator console — sees exactly what to
fix, never an opaque 4xx.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Why the prospect is being reviewed *now* — this IS the intent audit trail.
#   signal_decay       — the internal DecayRiskScore crossed its threshold
#   manual_review      — operator (or peer) asked for a review explicitly
#   macro_market_shift — an external market/regulatory event forced a look
#   reengagement       — the soft-no re-engagement sweep is resurfacing a
#                        prospect whose last_outcome was "not_interested" past
#                        the cooldown window. The outreach stage reads this
#                        source from CashEngineState and routes through the
#                        promotional template instead of the cold opener.
#   auto_stake_sweep   — the control_tick auto-stake sweep promoted a
#                        prospect that met the qualification bar (lead_score
#                        >= threshold, owner_email present) to a staked
#                        opportunity. The audit trail distinguishes these
#                        autonomous-pipeline runs from operator-initiated
#                        reviews — relevant for spend attribution and the
#                        send-ramp accounting.
TriggerSource = Literal[
    "signal_decay", "manual_review", "macro_market_shift", "reengagement",
    "auto_stake_sweep",
]

# Terminal disposition of a review.
#   enqueued  — both gates cleared; the downstream sequence was queued
#   escalated — a gate blocked; surfaced to the operator, nothing executed
#   disabled  — cash_engine_enabled is False; the engine is parked
#   invalid   — the request could not be acted on (e.g. no Opportunity)
ReviewStatus = Literal["enqueued", "escalated", "disabled", "invalid"]


class RevenueTriggerRequest(BaseModel):
    """Raw intent presented at the Cash Engine front door."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str = Field(min_length=1)
    trigger_source: TriggerSource
    # Samus's best guess at the prospect's status when it flagged the crisis
    # point — e.g. "High Signal, Low Engagement Risk". Free text; recorded on
    # the envelope + ledger for the operator's context, never trusted as a
    # gate input.
    current_samus_state: str = ""
    # Optional human-readable elaboration of the trigger (decay detail, the
    # operator's note, the market event). Audit colour only.
    trigger_reason: str = ""
    # If a Gap Report artifact already exists for this prospect, the caller
    # may thread its id so the downstream sequence skips re-auditing. Absent
    # is the common case (the sequence will produce one).
    gap_report_artifact_id: str = ""


class RevenueTriggerResult(BaseModel):
    """Structured verdict returned by ``review_opportunity``."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    status: ReviewStatus
    prospect_id: str = ""
    opportunity_id: str = ""
    # Set when status == "enqueued": the minted job id + where it landed
    # ("sqs:<service>" or "mock:jsonl").
    task_id: str = ""
    queue: str = ""
    # Set when status == "escalated": the precondition that blocked the door.
    # ``required_protocol`` is a missing precondition ("stake_sentence",
    # "opportunity"); ``violated_rule_id`` is a Codex rule id ("VR-G2", ...).
    required_protocol: str | None = None
    violated_rule_id: str | None = None
    reason: str | None = None
    drafted_adr_path: str | None = None
    # The DecayRiskScore that fired a signal_decay trigger, when known.
    decay_risk: float | None = None


__all__ = [
    "TriggerSource",
    "ReviewStatus",
    "RevenueTriggerRequest",
    "RevenueTriggerResult",
]
