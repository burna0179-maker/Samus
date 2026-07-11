#!/usr/bin/env python3
"""
Alfred — Business Document Generation Agent
Source: ChatGPT recovery chat 37

Canonical relationship:
- [NEW pod] business/document_generation — Alfred command system
- [EXPANDS §6 agents] domain-specific pod with template registry
- [NEW] placeholder-substitution document generation w/ folder taxonomy

Trigger pattern (natural-language commands):
  "Alfred, generate a full proposal for {client} for {scope}."
  "Alfred, create a service agreement for SMMS client on $1,997 plan."
  "Alfred, build a 6-month financial projection with $8k baseline target."
  "Alfred, generate meeting minutes for this morning's strategy call."

Standardized placeholder system (used in ALL templates):
  {{company_name}} {{client_name}} {{project_name}} {{deliverables}}
  {{timeline}} {{pricing}} {{scope}} {{terms}} {{notes}} {{signature}} {{date}}
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Template taxonomy — 10 categories × ~8 templates each
# ---------------------------------------------------------------------------

TEMPLATE_TAXONOMY = {
    "proposals": [
        "proposal_master.md", "short_form_proposal.md",
        "ai_automation_proposal.md", "ugc_content_proposal.md",
        "social_media_management_proposal.md", "brand_identity_proposal.md",
        "sales_pitch_brief.md", "influencer_campaign_proposal.md",
        "saas_implementation_proposal.md", "monthly_retainer_proposal.md",
    ],
    "contracts": [
        "service_agreement.md", "influencer_partnership_agreement.md",
        "nda_mutual.md", "nda_one_way.md", "contractor_agreement.md",
        "content_rights_transfer_agreement.md", "data_processing_addendum.md",
        "subscription_terms.md", "late_payment_clause.md", "refund_policy.md",
    ],
    "sop": [
        "sop_master_template.md", "sop_smms_client_onboarding.md",
        "sop_smms_daily_execution.md", "sop_ugc_review_approval.md",
        "sop_content_delivery_pipeline.md", "sop_sales_call_process.md",
        "sop_lead_generation.md", "sop_client_renewal.md",
        "sop_crisis_management.md", "sop_ai_review_and_containment.md",
    ],
    "strategy": [
        "business_plan_full.md", "business_plan_one_page.md",
        "competitive_analysis.md", "swot_analysis.md", "go_to_market_plan.md",
        "quarterly_strategy_brief.md", "org_chart_template.md",
        "pricing_strategy.md", "positioning_statement.md", "brand_voice_guide.md",
    ],
    "marketing": [
        "marketing_brief.md", "creative_direction_brief.md", "brand_kit.md",
        "campaign_planner.md", "content_calendar_weekly.md",
        "content_calendar_monthly.md", "ideal_customer_profile.md",
        "persona_template.md", "influencer_brief_nova_hart.md",
        "ugc_script_template.md",
    ],
    "sales": [
        "sales_script_12min_call.md", "sales_objection_list.md",
        "dm_outreach_script.md", "closing_script.md",
        "followup_sequence.md", "lead_qualification_sheet.md",
        "sales_pipeline_template.md", "offer_stack_template.md", "upsell_menu.md",
    ],
    "hr": [
        "job_description_template.md", "employee_handbook_outline.md",
        "contractor_orientation_brief.md", "code_of_conduct.md",
        "performance_review_template.md", "employee_onboarding_checklist.md",
    ],
    "finance": [
        "professional_invoice.md", "estimate_template.md",
        "financial_projection_6_month.md", "cash_flow_sheet.md",
        "profit_loss_template.md", "budget_template.md",
    ],
    "reports": [
        "weekly_report.md", "monthly_report.md", "client_performance_report.md",
        "social_media_analytics_report.md", "content_performance_report.md",
        "ai_operations_report.md", "timeline_update.md",
    ],
    "admin": [
        "meeting_agenda.md", "meeting_minutes.md", "task_brief.md",
        "project_kickoff_brief.md", "handover_document.md",
        "risk_assessment.md", "change_request_form.md",
    ],
}


# ---------------------------------------------------------------------------
# Intent classifier (natural language → template selection)
# ---------------------------------------------------------------------------

INTENT_PATTERNS = {
    "proposals/ai_automation_proposal.md": [
        r"proposal.*ai.*automation", r"automation.*proposal",
    ],
    "proposals/proposal_master.md": [
        r"full proposal", r"generate.*proposal", r"new proposal",
    ],
    "contracts/service_agreement.md": [
        r"service agreement", r"sla", r"client contract",
    ],
    "contracts/nda_mutual.md": [
        r"mutual nda", r"both-side nda",
    ],
    "sop/sop_smms_daily_execution.md": [
        r"smms daily", r"daily ops sop", r"daily execution sop",
    ],
    "sop/sop_smms_client_onboarding.md": [
        r"client.onboarding", r"onboarding sop",
    ],
    "marketing/marketing_brief.md": [
        r"marketing brief",
    ],
    "marketing/influencer_brief_nova_hart.md": [
        r"nova.hart", r"influencer brief",
    ],
    "finance/financial_projection_6_month.md": [
        r"6.?month.*projection", r"financial projection",
    ],
    "finance/professional_invoice.md": [
        r"invoice",
    ],
    "admin/meeting_minutes.md": [
        r"meeting minutes",
    ],
    "admin/meeting_agenda.md": [
        r"meeting agenda",
    ],
}


class GenerationStatus(str, Enum):
    READY = "ready"
    TEMPLATE_NOT_FOUND = "template_not_found"
    MISSING_PLACEHOLDERS = "missing_placeholders"
    GENERATED = "generated"


@dataclass
class DocumentRequest:
    user_command: str
    placeholders: Dict[str, str] = field(default_factory=dict)
    output_dir: Optional[Path] = None


@dataclass
class DocumentResult:
    status: GenerationStatus
    template_path: Optional[str] = None
    output_path: Optional[Path] = None
    missing_placeholders: List[str] = field(default_factory=list)
    rendered_text: str = ""


class AlfredDocumentAgent:
    POD_ID = "alfred"

    PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")

    def __init__(self, template_root: Path):
        self.template_root = Path(template_root)

    def classify_intent(self, command: str) -> Optional[str]:
        """Natural language → template path."""
        text = command.lower()
        for tpl_path, patterns in INTENT_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, text):
                    return tpl_path
        return None

    def find_placeholders(self, template_text: str) -> List[str]:
        return list(set(self.PLACEHOLDER_PATTERN.findall(template_text)))

    def render(self, template_text: str, values: Dict[str, str]) -> str:
        def repl(m):
            key = m.group(1)
            return str(values.get(key, m.group(0)))
        return self.PLACEHOLDER_PATTERN.sub(repl, template_text)

    def generate(self, req: DocumentRequest) -> DocumentResult:
        tpl_path = self.classify_intent(req.user_command)
        if not tpl_path:
            return DocumentResult(status=GenerationStatus.TEMPLATE_NOT_FOUND)

        full_path = self.template_root / tpl_path
        if not full_path.exists():
            return DocumentResult(status=GenerationStatus.TEMPLATE_NOT_FOUND,
                                  template_path=tpl_path)

        template_text = full_path.read_text(encoding="utf-8")
        required = self.find_placeholders(template_text)
        missing = [p for p in required if p not in req.placeholders]

        if missing:
            # Auto-fill common values; flag remaining
            auto_fills = {
                "date": time.strftime("%Y-%m-%d"),
                "company_name": "HustleForge",
            }
            for k, v in auto_fills.items():
                if k in missing:
                    req.placeholders[k] = v
                    missing.remove(k)

        if missing:
            return DocumentResult(status=GenerationStatus.MISSING_PLACEHOLDERS,
                                  template_path=tpl_path,
                                  missing_placeholders=missing)

        rendered = self.render(template_text, req.placeholders)
        out_path = None
        if req.output_dir:
            req.output_dir.mkdir(parents=True, exist_ok=True)
            out_path = req.output_dir / Path(tpl_path).name
            out_path.write_text(rendered, encoding="utf-8")

        return DocumentResult(
            status=GenerationStatus.GENERATED,
            template_path=tpl_path,
            output_path=out_path,
            rendered_text=rendered,
        )

    def scaffold_template_tree(self, dest: Optional[Path] = None) -> Dict[str, List[Path]]:
        """Create the 10-category folder structure with empty placeholder files."""
        dest = dest or self.template_root
        created: Dict[str, List[Path]] = {}
        for category, files in TEMPLATE_TAXONOMY.items():
            cat_dir = dest / category
            cat_dir.mkdir(parents=True, exist_ok=True)
            created[category] = []
            for f in files:
                fp = cat_dir / f
                if not fp.exists():
                    fp.write_text(f"# {f.replace('.md', '').replace('_', ' ').title()}\n\n<!-- TEMPLATE: TODO fill in -->\n", encoding="utf-8")
                created[category].append(fp)
        return created
