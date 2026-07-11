"""Tests for backend.workflow.compiler + node_library — TaskPlan -> n8n JSON.

Fully offline + deterministic (use_llm defaults off). Builds the plan via the
real scope_planner so the test exercises the same path fulfillment uses.
"""

from __future__ import annotations

from backend.services.scope_planner import TaskPlan, generate_scope
from backend.workflow.compiler import compile_workflow


def _rescue_plan() -> TaskPlan:
    intake = {
        "email": "t@x.com",
        "bottleneck": (
            "Calendly booking creates a Stripe invoice, appends a row to a Google "
            "Sheet, sends an SMS to the tech via Twilio, and pings Discord on failure"
        ),
    }
    return generate_scope(intake, "service_workflow_rescue").plan


def test_compile_has_exactly_one_trigger():
    wf = compile_workflow(_rescue_plan(), name="Test")
    triggers = [n for n in wf.nodes if n.kind == "trigger"]
    assert len(triggers) == 1


def test_compile_maps_known_tools_to_native_nodes():
    wf = compile_workflow(_rescue_plan(), name="Test")
    types = {n.type for n in wf.nodes}
    # booking_created -> webhook trigger; twilio sms; google sheets append.
    assert "n8n-nodes-base.webhook" in types
    assert "n8n-nodes-base.twilio" in types
    assert "n8n-nodes-base.googleSheets" in types


def test_compile_always_adds_failure_branch():
    wf = compile_workflow(_rescue_plan(), name="Test")
    kinds = [n.kind for n in wf.nodes]
    assert "error_trigger" in kinds
    err = next(n for n in wf.nodes if n.kind == "error_trigger")
    # The error trigger is wired to a failure-alert node.
    assert err.name in wf.connections
    targets = [l["node"] for l in wf.connections[err.name]["main"][0]]
    assert any("Failure Alert" in t for t in targets)


def test_compile_to_dict_is_valid_n8n_shape():
    wf = compile_workflow(_rescue_plan(), name="Test")
    d = wf.to_dict()
    assert set(d) >= {"name", "nodes", "connections", "settings"}
    assert d["settings"]["executionOrder"] == "v1"
    # Every node dict has the required n8n keys.
    for node in d["nodes"]:
        assert set(node) >= {"id", "name", "type", "typeVersion", "position", "parameters"}
        assert isinstance(node["position"], list) and len(node["position"]) == 2


def test_compile_crm_is_tool_aware():
    intake = {"email": "t@x.com", "bottleneck": "new lead from form -> create a HubSpot contact"}
    plan = generate_scope(intake, "service_workflow_rescue").plan
    wf = compile_workflow(plan, name="Test")
    assert "n8n-nodes-base.hubspot" in {n.type for n in wf.nodes}


def test_compile_unknown_action_falls_back_to_http():
    # A plan with an action label not in the library still compiles.
    plan = TaskPlan(
        triggers=["form_submission"], actions=["frobnicate_widgets"], notifications=[], tools=[]
    )
    wf = compile_workflow(plan, name="Test")
    assert "n8n-nodes-base.httpRequest" in {n.type for n in wf.nodes}


def test_compile_empty_plan_defaults_to_webhook_trigger():
    wf = compile_workflow(TaskPlan(), name="Test")
    triggers = [n for n in wf.nodes if n.kind == "trigger"]
    assert len(triggers) == 1
    assert triggers[0].type == "n8n-nodes-base.webhook"


def test_node_names_are_unique():
    # Two send_email actions would collide without de-duping.
    plan = TaskPlan(
        triggers=["form_submission"],
        actions=["send_email", "send_email"],
        notifications=[],
        tools=[],
    )
    wf = compile_workflow(plan, name="Test")
    names = [n.name for n in wf.nodes]
    assert len(names) == len(set(names))
