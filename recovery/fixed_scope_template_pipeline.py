#!/usr/bin/env python3
"""
Fixed-Scope Automation Template Pipeline — agentic system for $500 service offer
Source: ChatGPT recovery chat 43

Canonical relationship:
- [NEW pack] business/automation — converts intake → validated workflow from approved templates
- [EXPANDS §6 agents] 5-stage hierarchical pipeline (Planner → Selector → Compiler → Deploy → Validate)
- [PAIRS WITH] alfred_document_agent.py (template-registry pattern, NL intent classifier)
- [PAIRS WITH] master_framework_mutation_lifecycle.py (template-promotion lifecycle)

Pipeline:
   Intake → Planner → Template Selector → Workflow Compiler → Deployment → Validation → Telemetry

Strict scope guardrails (fixed-price service):
   MAX_WORKFLOW_STEPS  = 5
   MAX_EXTERNAL_TOOLS  = 3
   MAX_TEMPLATES       = 3
   → exceeds = OUT_OF_SCOPE → refund_protocol()
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


MAX_WORKFLOW_STEPS = 5
MAX_EXTERNAL_TOOLS = 3
MAX_TEMPLATES = 3


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


@dataclass
class TaskPlan:
    triggers: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    notifications: List[str] = field(default_factory=list)


@dataclass
class TemplateDefinition:
    template_id: str
    type: str                                     # trigger | action | notification
    description: str
    tags: List[str] = field(default_factory=list)
    supported_tools: List[str] = field(default_factory=list)
    supported_triggers: List[str] = field(default_factory=list)
    supported_actions: List[str] = field(default_factory=list)
    supported_notifications: List[str] = field(default_factory=list)
    reliability_score: float = 0.5
    maturity: TemplateMaturity = TemplateMaturity.EXPERIMENTAL


@dataclass
class CompiledWorkflow:
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, str]] = field(default_factory=list)
    total_steps: int = 0


@dataclass
class DeploymentJob:
    job_id: str
    customer_email: str
    task_description: str
    tool_stack: List[str]
    stage: PipelineStage = PipelineStage.PENDING_INTAKE
    plan: Optional[TaskPlan] = None
    selected_templates: Dict[str, Any] = field(default_factory=dict)
    workflow: Optional[CompiledWorkflow] = None
    validation_passed: bool = False
    out_of_scope_reason: Optional[str] = None
    created_at: float = field(default_factory=time.time)


# ----- Planner Agent (LLM-driven) -----
def generate_plan(task_description: str, tools: List[str], llm_call) -> TaskPlan:
    """LLM produces structured plan from NL description. llm_call: Callable[[str], dict]."""
    prompt = f"""
Break this automation task into a structured plan.
Task: {task_description}
Tools: {tools}
Return JSON: {{"triggers": [], "actions": [], "notifications": []}}
"""
    raw = llm_call(prompt)
    return TaskPlan(**raw)


# ----- Template Selector -----
def select_templates(plan: TaskPlan, registry: List[TemplateDefinition]) -> Dict[str, Any]:
    selected: Dict[str, Any] = {"trigger": None, "actions": [], "notifications": []}
    for tpl in registry:
        if tpl.type == "trigger":
            if plan.triggers and plan.triggers[0] in tpl.supported_triggers:
                selected["trigger"] = tpl.template_id
        elif tpl.type == "action":
            for action in plan.actions:
                if action in tpl.supported_actions:
                    selected["actions"].append(tpl.template_id)
        elif tpl.type == "notification":
            for note in plan.notifications:
                if note in tpl.supported_notifications:
                    selected["notifications"].append(tpl.template_id)
    return selected


# ----- Workflow Compiler -----
def compile_workflow(templates: Dict[str, Any]) -> CompiledWorkflow:
    nodes, edges = [], []
    trigger = templates["trigger"]
    if not trigger:
        return CompiledWorkflow()
    nodes.append({"id": "trigger", "template": trigger})
    prev = "trigger"
    for i, action in enumerate(templates["actions"]):
        node_id = f"action_{i}"
        nodes.append({"id": node_id, "template": action})
        edges.append({"from": prev, "to": node_id})
        prev = node_id
    for i, note in enumerate(templates["notifications"]):
        node_id = f"notify_{i}"
        nodes.append({"id": node_id, "template": note})
        edges.append({"from": prev, "to": node_id})
    return CompiledWorkflow(nodes=nodes, edges=edges, total_steps=len(nodes))


# ----- Scope Validator -----
def validate_scope(workflow: CompiledWorkflow, tool_stack: List[str], template_count: int) -> Optional[str]:
    """Returns None if in scope, else reason string."""
    if workflow.total_steps > MAX_WORKFLOW_STEPS:
        return f"workflow_steps_exceeded: {workflow.total_steps} > {MAX_WORKFLOW_STEPS}"
    if len(tool_stack) > MAX_EXTERNAL_TOOLS:
        return f"tools_exceeded: {len(tool_stack)} > {MAX_EXTERNAL_TOOLS}"
    if template_count > MAX_TEMPLATES:
        return f"templates_exceeded: {template_count} > {MAX_TEMPLATES}"
    return None


# ----- Telemetry / Learning Engine -----
@dataclass
class TelemetryRecord:
    job_id: str
    task_description: str
    templates_used: List[str]
    tools: List[str]
    execution_status: str
    latency_seconds: float
    validation_passed: bool
    timestamp: float = field(default_factory=time.time)


class TemplateLearningEngine:
    """
    Detect recurring workflow patterns from telemetry → propose composite templates.
    Production safe: only proposes; promotion requires explicit approval.
    """
    PATTERN_THRESHOLD = 20         # pattern must appear N times
    MIN_PATTERN_COMPLEXITY = 2     # min nodes in pattern

    def detect_candidates(self, history: List[TelemetryRecord]) -> List[tuple]:
        counter = collections.Counter()
        for rec in history:
            if rec.validation_passed:
                counter[tuple(rec.templates_used)] += 1
        return [
            pattern for pattern, count in counter.items()
            if count >= self.PATTERN_THRESHOLD and len(pattern) >= self.MIN_PATTERN_COMPLEXITY
        ]

    @staticmethod
    def score_template(template_id: str, history: List[TelemetryRecord]) -> float:
        """Reliability score: success_rate * 0.5 + validation_rate * 0.3 + low_latency * 0.2"""
        uses = [r for r in history if template_id in r.templates_used]
        if not uses:
            return 0.5
        success_rate = sum(1 for r in uses if r.execution_status == "success") / len(uses)
        validation_rate = sum(1 for r in uses if r.validation_passed) / len(uses)
        avg_latency = sum(r.latency_seconds for r in uses) / len(uses)
        low_latency_score = max(0.0, min(1.0, (10.0 - avg_latency) / 10.0))
        return success_rate * 0.5 + validation_rate * 0.3 + low_latency_score * 0.2


# ----- Promotion gates -----
PROMOTION_CRITERIA = {
    TemplateMaturity.EXPERIMENTAL: {"min_successful_runs": 0, "min_validation_rate": 0.0},
    TemplateMaturity.CANDIDATE:    {"min_successful_runs": 5, "min_validation_rate": 0.80},
    TemplateMaturity.VERIFIED:     {"min_successful_runs": 10, "min_validation_rate": 0.95},
    TemplateMaturity.PRODUCTION:   {"min_successful_runs": 50, "min_validation_rate": 0.97},
}


def can_promote(current: TemplateMaturity, runs: int, validation_rate: float) -> Optional[TemplateMaturity]:
    """Return next maturity if promotion criteria met."""
    next_levels = list(TemplateMaturity)
    idx = next_levels.index(current)
    if idx + 1 >= len(next_levels):
        return None
    target = next_levels[idx + 1]
    crit = PROMOTION_CRITERIA[target]
    if runs >= crit["min_successful_runs"] and validation_rate >= crit["min_validation_rate"]:
        return target
    return None


# ----- Drift detection (template degradation) -----
DRIFT_THRESHOLD = 0.80


def detect_drift(template_id: str, history: List[TelemetryRecord]) -> bool:
    """Mark template as degraded if reliability drops below threshold."""
    score = TemplateLearningEngine.score_template(template_id, history)
    return score < DRIFT_THRESHOLD


# ----- Full pipeline orchestrator -----
class AutomationPipeline:
    def __init__(self, registry: List[TemplateDefinition], llm_call):
        self.registry = registry
        self.llm_call = llm_call
        self.learning = TemplateLearningEngine()

    def execute(self, job: DeploymentJob) -> DeploymentJob:
        # 1. Plan
        job.stage = PipelineStage.DESIGNING
        job.plan = generate_plan(job.task_description, job.tool_stack, self.llm_call)

        # 2. Select templates
        job.stage = PipelineStage.BUILDING
        job.selected_templates = select_templates(job.plan, self.registry)
        template_count = sum([
            1 if job.selected_templates.get("trigger") else 0,
            len(job.selected_templates.get("actions", [])),
            len(job.selected_templates.get("notifications", [])),
        ])

        # 3. Compile workflow
        job.workflow = compile_workflow(job.selected_templates)

        # 4. Scope validation (hard gate)
        oos = validate_scope(job.workflow, job.tool_stack, template_count)
        if oos:
            job.stage = PipelineStage.OUT_OF_SCOPE
            job.out_of_scope_reason = oos
            return job

        # 5. Deploy (skeleton — wire to actual deployment backend)
        job.stage = PipelineStage.DEPLOYING

        # 6. Validate (skeleton — wire to validation runner)
        job.stage = PipelineStage.TESTING
        job.validation_passed = True   # TODO: actual test run

        if job.validation_passed:
            job.stage = PipelineStage.DELIVERED

        return job
