"""Pydantic models for the proposal workcell."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PipelineStage(str, Enum):
    PENDING_INTAKE = "pending_intake"
    DESIGNING = "designing"
    BUILDING = "building"
    DEPLOYING = "deploying"
    TESTING = "testing"
    DELIVERED = "delivered"
    OUT_OF_SCOPE = "out_of_scope"


class TemplateMaturity(str, Enum):
    EXPERIMENTAL = "experimental"
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    PRODUCTION = "production"


class TemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    type: Literal["trigger", "action", "notification"]
    description: str
    tags: list[str] = Field(default_factory=list)
    supported_tools: list[str] = Field(default_factory=list)
    supported_triggers: list[str] = Field(default_factory=list)
    supported_actions: list[str] = Field(default_factory=list)
    supported_notifications: list[str] = Field(default_factory=list)
    reliability_score: float = 0.5
    maturity: TemplateMaturity = TemplateMaturity.EXPERIMENTAL


class OnboardingIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str
    business_goal: str
    triggers_wanted: list[str] = Field(default_factory=list)
    actions_wanted: list[str] = Field(default_factory=list)
    notifications_wanted: list[str] = Field(default_factory=list)
    tools_available: list[str] = Field(default_factory=list)
    budget_usd: float | None = None


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triggers: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    notifications: list[str] = Field(default_factory=list)


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    kind: Literal["trigger", "action", "notification"]
    template_id: str
    description: str = ""


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: str
    target_node_id: str


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    total_steps: int = 0
    used_tools: list[str] = Field(default_factory=list)


class ProposalValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passes: bool
    reasons: list[str] = Field(default_factory=list)


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    intake: OnboardingIntake
    # Phase 5 — optional CRM linkage. When either is supplied the proposal-
    # completion path fires a best-effort artifact registration to samus-crm.
    # opportunity_id takes precedence (owner_entity_kind='opportunity');
    # else prospect_id is used. Both blank -> no dispatch.
    opportunity_id: str = ""
    prospect_id: str = ""


class ProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    stage: PipelineStage
    plan: TaskPlan | None = None
    workflow: CompiledWorkflow | None = None
    validation: ProposalValidation
    refund_protocol: bool = False
    status: Literal["approved", "out_of_scope", "needs_review"]
    cache_hit: bool = False
