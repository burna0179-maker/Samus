"""Declarative campaign graph model — the config-driven vocabulary.

Every vertical campaign (school enrollment, medical patient acquisition, law
firm lead-gen, contractor estimates, nonprofit fundraising, local-business
promotion) is expressed as a :class:`CampaignTemplate` graph of
:class:`CampaignNode` stages joined by :class:`CampaignEdge` transitions. A
client-specific run is a :class:`CampaignInstance` bound to that template; its
live execution is a :class:`CampaignRun`.

Nothing here dispatches or executes — these are pure Pydantic value objects so
templates load, validate, and round-trip identically whether they came from
YAML on disk, an API body, or a test fixture. Execution lives in
``executor.py`` / ``orchestrator.py``; routing rules live in ``registry.py``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.common.dates import iso_now


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class CampaignState(str, Enum):
    """Lifecycle state of a whole campaign run (deliverable §4)."""

    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    """Per-node execution status inside a run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class ApprovalLevel(str, Enum):
    """Who must say yes before a node may execute (deliverable §8)."""

    NONE = "none"
    OPERATOR = "operator"
    CLIENT = "client"
    GOVERNANCE = "governance"


class AuditSeverity(str, Enum):
    """Severity tag emitted with every node audit event (deliverable §7)."""

    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class RollbackMode(str, Enum):
    """What to do with a node that fails after exhausting its retry policy."""

    NONE = "none"  # leave the node failed, continue siblings
    HALT = "halt"  # stop the run (state -> failed)
    COMPENSATE = "compensate"  # dispatch a compensating capability, then halt


# ---------------------------------------------------------------------------
# Policy value objects
# ---------------------------------------------------------------------------


class RetryPolicy(BaseModel):
    """Bounded retry with exponential backoff for a single node."""

    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0.0, ge=0.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    # Reasons the executor is allowed to retry on. Empty = retry any failure.
    retry_on: list[str] = Field(default_factory=list)


class RollbackPolicy(BaseModel):
    """Rollback behaviour when a node ultimately fails."""

    mode: RollbackMode = RollbackMode.HALT
    # Capability dispatched to ``target_workcell`` to compensate (mode=compensate).
    compensation_capability: str | None = None


# ---------------------------------------------------------------------------
# Graph elements
# ---------------------------------------------------------------------------


class CampaignNode(BaseModel):
    """One ordered stage in a campaign graph.

    A node names a *capability* to run on a *target workcell*; the orchestrator
    routes it there through the signed gateway seam. ``input_mapping`` /
    ``output_mapping`` project the run context into the dispatch payload and the
    node result back into KPIs / artifacts.
    """

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    target_workcell: str = Field(min_length=1)
    capability: str = Field(min_length=1)

    approval_required: ApprovalLevel = ApprovalLevel.NONE
    audit_severity: AuditSeverity | None = None  # None -> defaulted from type

    input_mapping: dict[str, Any] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)
    kpis: list[str] = Field(default_factory=list)

    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    rollback: RollbackPolicy = Field(default_factory=RollbackPolicy)

    title: str = ""
    description: str = ""

    @field_validator("id")
    @classmethod
    def _no_whitespace_id(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("node id must not contain whitespace")
        return v


class CampaignEdge(BaseModel):
    """A directed transition between two nodes.

    An optional ``condition`` makes the edge a *conditional* branch — it is only
    traversable when the condition evaluates true against the run context (see
    ``graph.evaluate_condition``). No condition = an unconditional dependency.
    """

    from_node: str = Field(min_length=1, alias="from")
    to_node: str = Field(min_length=1, alias="to")
    condition: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Descriptive value objects (audience / channels / KPIs / vertical rules)
# ---------------------------------------------------------------------------


class CampaignChannel(BaseModel):
    """A marketing channel the campaign publishes/operates through."""

    name: str = Field(min_length=1)
    kind: str = "social"  # social|email|sms|flyer|web|ads|review|partner|search
    handle: str = ""  # @handle, page url, list name, ...
    cadence: str = ""  # freeform cadence, e.g. "3/week", "biweekly"
    enabled: bool = True


class CampaignAudience(BaseModel):
    """Who the campaign targets, and where discovery sources it from."""

    name: str = "primary"
    geography: list[str] = Field(default_factory=list)
    radius_miles: float | None = None
    segments: list[str] = Field(default_factory=list)
    source_workcell: str = "prospecting"


class CampaignKPI(BaseModel):
    """A KPI *definition* (not a value) attached to a template."""

    key: str = Field(min_length=1)
    label: str = ""
    unit: str = "count"
    target: float | None = None
    # "up" = higher is better (default), "down" = lower is better.
    direction: str = "up"


class CampaignVerticalRules(BaseModel):
    """Client/vertical-specific governance overrides layered onto a template.

    These let one template serve many verticals while still honouring, e.g.,
    stricter approval requirements for a medical vertical or forbidden
    capabilities for a regulated one.
    """

    vertical: str = ""
    # node_type -> required ApprovalLevel override (raises the floor only).
    approval_overrides: dict[str, ApprovalLevel] = Field(default_factory=dict)
    # capabilities that must never run for this vertical.
    forbidden_capabilities: list[str] = Field(default_factory=list)
    # highest audit severity allowed to auto-run without approval.
    max_auto_severity: AuditSeverity = AuditSeverity.NOTICE
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Template + instance
# ---------------------------------------------------------------------------


class CampaignTemplate(BaseModel):
    """A reusable, vertical-specific campaign graph loaded from config."""

    template_id: str = Field(min_length=1)
    vertical: str = Field(min_length=1)
    name: str = ""
    version: str = "1.0.0"

    required_inputs: list[str] = Field(default_factory=list)
    kpis: list[CampaignKPI] = Field(default_factory=list)
    nodes: list[CampaignNode] = Field(default_factory=list)
    edges: list[CampaignEdge] = Field(default_factory=list)

    audience: CampaignAudience | None = None
    channels: list[CampaignChannel] = Field(default_factory=list)
    vertical_rules: CampaignVerticalRules | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)

    def node_map(self) -> dict[str, CampaignNode]:
        return {n.id: n for n in self.nodes}


class CampaignInstance(BaseModel):
    """A client-specific binding of a template — config, not code."""

    campaign_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    vertical: str = ""

    inputs: dict[str, Any] = Field(default_factory=dict)
    channels: list[CampaignChannel] = Field(default_factory=list)
    audience: CampaignAudience | None = None
    kpi_targets: dict[str, float] = Field(default_factory=dict)
    # per-node overrides keyed by node id: {"publish_social_content": {...}}
    node_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: str = Field(default_factory=iso_now)


# ---------------------------------------------------------------------------
# Run-time records
# ---------------------------------------------------------------------------


class CampaignArtifact(BaseModel):
    """A reference to an output the campaign produced.

    Only a *reference* is stored (URI, path, external id) plus a content hash —
    never the raw media — so the run state and audit ledger stay small and
    PII/secret-free.
    """

    artifact_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    ref: str = ""
    node_id: str = ""
    content_hash: str = ""
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=iso_now)


class CampaignStepResult(BaseModel):
    """The outcome of executing one node in a run."""

    node_id: str = Field(min_length=1)
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float = 0.0
    output_hash: str = ""
    output_summary: dict[str, Any] = Field(default_factory=dict)
    error_summary: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    kpi_refs: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    trace_id: str | None = None


class CampaignRun(BaseModel):
    """Live execution state of a campaign (deliverable §4).

    This is the single source of truth the API surfaces expose and the weekly
    report is assembled from — persisted through ``store.CampaignRunStore``.
    """

    campaign_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    vertical: str = ""

    state: CampaignState = CampaignState.DRAFT
    current_node: str | None = None
    completed_nodes: list[str] = Field(default_factory=list)
    blocked_nodes: list[str] = Field(default_factory=list)
    failed_nodes: list[str] = Field(default_factory=list)
    needs_approval_nodes: list[str] = Field(default_factory=list)

    artifacts_created: list[CampaignArtifact] = Field(default_factory=list)
    kpis_updated: dict[str, float] = Field(default_factory=dict)
    approvals_required: list[str] = Field(default_factory=list)
    audit_events: list[str] = Field(default_factory=list)

    step_results: dict[str, CampaignStepResult] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    created_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)

    def touch(self) -> None:
        self.updated_at = iso_now()
