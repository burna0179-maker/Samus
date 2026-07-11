"""Samus n8n workflow-generation engine — runnable deliverables for the
workflow-automation SKUs (48-Hour Rescue / System Buildout / AI Ops Partner).

HustleForge's flagship line is workflow automation, and the service layer already
parses a customer's intake into a structured plan
(:class:`backend.services.scope_planner.TaskPlan` — triggers / actions /
notifications / tools). Until now that plan only produced a prose ``scope.md`` and
an operator playbook; the actual workflow was hand-built. This package closes that
gap: it **compiles the TaskPlan into a deployable n8n workflow JSON** (plus a
runbook), so the deliverable is the runnable thing, not a description of it.

Modules:

* :mod:`backend.workflow.models` — n8n JSON shapes (``N8nNode`` / ``N8nWorkflow``).
* :mod:`backend.workflow.node_library` — maps the scope vocabulary to concrete,
  tool-aware n8n node specs (with an ``httpRequest`` fallback so a plan always compiles).
* :mod:`backend.workflow.compiler` — lays out + wires the nodes and always appends a
  failure-alert branch (the HustleForge quality signature). Optional, budget-gated
  LLM parameter enrichment (fail-soft to deterministic).
* :mod:`backend.workflow.validate` — structural validation (one trigger, unique
  names, connection integrity, orphan detection, credential report).
* :mod:`backend.workflow.runbook` — the documented runbook deliverable.
* :mod:`backend.workflow.deploy` — DORMANT, dry-run-by-default push to an n8n
  instance via its REST API.
* :mod:`backend.workflow.service` — the single ``generate_workflow_deliverable`` entry point.

**Licensing:** n8n is fair-code (Sustainable Use License). We generate workflow
JSON *data* the customer imports into *their own* n8n, and optionally call n8n's
public REST API — we do not vendor, bundle, or redistribute n8n source. Generation
is pure/offline/$0 and defaults ON; live deploy + LLM enrichment are dormant.
"""
