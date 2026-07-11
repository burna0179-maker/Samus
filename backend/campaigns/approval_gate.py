"""Reusable approval gate — pause a campaign at a node until a human says yes.

Composes the canonical TTL-bounded HOTL queue in ``backend.common.approvals``
(ADR-0019 severity + fail-closed expiry) instead of forking a parallel approval
scheme. The orchestrator asks this module two things:

  * :func:`resolve_level` — what approval level does this node actually need,
    after folding the node-type floor and any vertical-rule override;
  * :func:`ensure` — is there a standing approval for this node? If not, create
    one and report "not yet approved" so the run pauses.

High-risk nodes (registry.is_high_risk) whose resolved level is ``none`` are a
config error the *executor* rejects; this gate only handles legitimate gates.
"""

from __future__ import annotations

from backend.common import approvals

from .models import (
    ApprovalLevel,
    AuditSeverity,
    CampaignNode,
    CampaignRun,
    CampaignVerticalRules,
)
from .registry import spec_for

APPROVAL_KIND = "campaign_node"

# Approval level ordering — a higher index is a stricter gate.
_LEVEL_ORDER = [
    ApprovalLevel.NONE,
    ApprovalLevel.OPERATOR,
    ApprovalLevel.CLIENT,
    ApprovalLevel.GOVERNANCE,
]


def _max_level(a: ApprovalLevel, b: ApprovalLevel) -> ApprovalLevel:
    return a if _LEVEL_ORDER.index(a) >= _LEVEL_ORDER.index(b) else b


def resolve_level(
    node: CampaignNode, vertical_rules: CampaignVerticalRules | None
) -> ApprovalLevel:
    """The effective approval level for a node (floor + overrides, raise-only).

    Takes the strictest of: the node's declared ``approval_required``, the
    node-type default, and any vertical-rule override for the node type. A
    vertical can only *raise* the requirement, never lower it.
    """
    level = _max_level(node.approval_required, spec_for(node.type).default_approval)
    if vertical_rules and node.type in vertical_rules.approval_overrides:
        level = _max_level(level, vertical_rules.approval_overrides[node.type])
    return level


def resolve_severity(node: CampaignNode) -> AuditSeverity:
    """Effective audit severity — the node's explicit value or the type default."""
    return node.audit_severity or spec_for(node.type).default_severity


def _risk_level_for(severity: AuditSeverity) -> str:
    """Map campaign audit severity -> approvals risk level (ADR-0019 tiers)."""
    if severity in (AuditSeverity.CRITICAL,):
        return "critical"
    if severity in (AuditSeverity.HIGH,):
        return "high"
    return "normal"


def ensure(run: CampaignRun, node: CampaignNode, level: ApprovalLevel) -> tuple[str, bool]:
    """Return ``(approval_id, approved)`` for this node's gate.

    Reuses an existing approval recorded on the step result when present;
    otherwise creates a new pending approval and returns ``approved=False`` so
    the caller pauses the run. Emergency/routine severity + TTL come from
    ``common.approvals`` per ADR-0019.
    """
    step = run.step_results.get(node.id)
    existing_id = step.approval_id if step else None
    if existing_id:
        return existing_id, approvals.is_currently_approved(existing_id)

    severity = resolve_severity(node)
    row = approvals.create_approval(
        APPROVAL_KIND,
        payload={
            "campaign_id": run.campaign_id,
            "client_id": run.client_id,
            "node_id": node.id,
            "node_type": node.type,
            "target_workcell": node.target_workcell,
            "capability": node.capability,
            "approval_level": level.value,
        },
        risk_level=_risk_level_for(severity),
    )
    approval_id = str(row.get("id") or "")
    return approval_id, False


def is_satisfied(approval_id: str | None) -> bool:
    """True iff the referenced approval is currently ``approved``."""
    if not approval_id:
        return False
    return approvals.is_currently_approved(approval_id)


def pending_by_severity(run: CampaignRun) -> dict[str, int]:
    """Count this run's still-pending approvals bucketed by ADR-0019 severity."""
    buckets: dict[str, int] = {}
    for approval_id in run.approvals_required:
        row = approvals.get_approval(approval_id)
        if row and row.get("status") == approvals.STATUS_PENDING:
            sev = str(row.get("severity") or approvals.SEVERITY_ROUTINE)
            buckets[sev] = buckets.get(sev, 0) + 1
    return buckets
