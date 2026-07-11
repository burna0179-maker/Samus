"""Campaign Orchestrator — declarative, config-driven campaign graphs.

A reusable orchestration layer that composes Samus's existing workcells
(prospecting, SEO/GEO, outreach, CRM, intake, scaffold/proposal content, and
the metrics/reporting/audit conventions) from a declarative YAML template.
Vertical campaigns — school enrollment, medical patient acquisition, law-firm
lead-gen, contractor estimates, nonprofit fundraising, local-business promotion
— are defined by configuration, never hardcoded workflows.

Public surface:

    from backend.campaigns import (
        CampaignOrchestrator, default_orchestrator,
        load_template, load_instance,
        CampaignTemplate, CampaignInstance, CampaignRun,
    )
"""

from __future__ import annotations

from .models import (
    ApprovalLevel,
    AuditSeverity,
    CampaignArtifact,
    CampaignAudience,
    CampaignChannel,
    CampaignEdge,
    CampaignInstance,
    CampaignKPI,
    CampaignNode,
    CampaignRun,
    CampaignState,
    CampaignStepResult,
    CampaignTemplate,
    CampaignVerticalRules,
    NodeStatus,
)
from .orchestrator import (
    CampaignError,
    CampaignOrchestrator,
    default_orchestrator,
    reset_default_orchestrator,
)
from .templates import (
    TemplateError,
    load_instance,
    load_template,
    load_template_by_id,
)

__all__ = [
    "ApprovalLevel",
    "AuditSeverity",
    "CampaignArtifact",
    "CampaignAudience",
    "CampaignChannel",
    "CampaignEdge",
    "CampaignError",
    "CampaignInstance",
    "CampaignKPI",
    "CampaignNode",
    "CampaignOrchestrator",
    "CampaignRun",
    "CampaignState",
    "CampaignStepResult",
    "CampaignTemplate",
    "CampaignVerticalRules",
    "NodeStatus",
    "TemplateError",
    "default_orchestrator",
    "load_instance",
    "load_template",
    "load_template_by_id",
    "reset_default_orchestrator",
]
