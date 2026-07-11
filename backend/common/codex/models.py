"""Pydantic models + plain dataclasses for the Codex Validation Layer."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


GuardrailStatus = Literal["enforced", "intent"]


ActionKind = Literal[
    "outreach_send",
    "opportunity_write",
    "proposal_generate",
    "gap_report_render",
    "voice_dial",
    "subscription_create",
    "reward_function_update",
    "stake_sentence_author",
    "other",
]


class Guardrail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    status: GuardrailStatus
    description: str
    where: tuple[str, ...] = Field(default_factory=tuple)
    what_it_stops: str = ""
    bypass_forbidden: bool = True


class BannedPhrase(BaseModel):
    model_config = ConfigDict(frozen=True)

    phrase: str
    source: str


class ADR(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    date: str
    title: str
    decision: str
    superseded_by: str | None = None


class ShutdownSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    signal_description: str
    action: str


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    capability: str
    action_kind: ActionKind
    payload: dict[str, Any] = Field(default_factory=dict)
    proposed_by: str
    correlation_id: str | None = None


class Verdict(BaseModel):
    allowed: bool
    violated_rule_id: str | None = None
    reason: str | None = None
    drafted_adr_path: str | None = None
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "ADR",
    "ActionKind",
    "BannedPhrase",
    "Guardrail",
    "GuardrailStatus",
    "ProposedAction",
    "ShutdownSignal",
    "Verdict",
]
