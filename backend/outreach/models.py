"""Pydantic models for the outreach workcell."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_STATES = Literal[
    "open", "pitch", "engage", "handle_objection",
    "close_attempt", "fallback", "exit",
]
_OUTCOMES = Literal[
    "closed", "failed", "interested", "not_interested",
    "callback", "voicemail",
]


class OutreachIntel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: dict[str, str] = Field(default_factory=dict)
    signals: list[str] = Field(default_factory=list)


class OutreachAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    current_state: _STATES
    user_input: str = ""
    intel: OutreachIntel
    objection_detected: bool = False
    objection_response: str | None = None


class OutreachStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_state: str
    next_state: str
    action: str
    primary_product: str
    secondary_product: str | None = None


class OutreachLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    outcome: _OUTCOMES
    product: str
    angle: str
    objection: str | None = None


class OutreachMetricsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_objections: list[tuple[str, int]] = Field(default_factory=list)
    best_products: list[tuple[str, int]] = Field(default_factory=list)
    angle_performance: dict[str, float] = Field(default_factory=dict)


class OutreachOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    outcome: str
    ts: str
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)


class OutreachMessageRequest(BaseModel):
    """Outbound message capability — email wired via SES; sms/call/voicemail still deferred."""
    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    channel: Literal["email", "sms", "call", "voicemail"]
    template_id: str
    body: str = ""
    # Optional HTML alternative (the impulse-purchase flyer). When set, it is
    # sent as the multipart text/html part alongside the plain-text ``body``;
    # empty means plain-text only. See backend.outreach.flyer.
    html_body: str = ""
    to: str | None = None
    subject: str | None = None
    # Optional prospect contact detail — carried through so a sent outreach
    # message can be recorded in the CRM with enough for the operator to place
    # the follow-up call. The outreach workcell otherwise knows the prospect
    # only by id + email, and nothing downstream can resolve the rest.
    company: str = ""
    phone: str = ""
    # Campaign attribution (HOTL Tranche 1) — carried first-class from
    # CampaignConfig through the send path so the unified business-event
    # ledger + conversion attribution can credit the exact campaign / A-B
    # arm. Empty = un-attributed (ad-hoc / legacy sends keep working).
    campaign_id: str = ""
    variant_arm_id: str = ""

    @model_validator(mode="after")
    def _require_email_fields(self) -> "OutreachMessageRequest":
        if self.channel == "email":
            missing = [
                name for name, value in (("to", self.to), ("subject", self.subject))
                if not (value and value.strip())
            ]
            if missing:
                raise ValueError(
                    f"channel='email' requires non-empty {', '.join(missing)}"
                )
        return self
