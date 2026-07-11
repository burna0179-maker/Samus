"""Proposal workcell - fixed-scope ($500) automation template pipeline.

Converts an :class:`OnboardingIntake` into a validated, in-scope
:class:`CompiledWorkflow` through five deterministic stages:

    Intake -> Planner -> Template Selector -> Workflow Compiler -> Validation

Scope guardrails are hard gates (per the recovered ChatGPT-43 doc):

    MAX_WORKFLOW_STEPS  = 5
    MAX_EXTERNAL_TOOLS  = 3
    MAX_TEMPLATES       = 3

Exceeding any guardrail -> ``ProposalResult.status == "out_of_scope"`` and
``refund_protocol = True``. The pipeline is pure Python: no external calls,
no LLM. Personalisation happens upstream during intake; the proposal cell
just templatises the workflow shape.
"""
