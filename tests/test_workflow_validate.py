"""Tests for backend.workflow.validate — structural checks on a workflow."""

from __future__ import annotations

from backend.services.scope_planner import TaskPlan
from backend.workflow.compiler import compile_workflow
from backend.workflow.models import N8nNode, N8nWorkflow
from backend.workflow.validate import is_valid, validate_workflow


def _good() -> N8nWorkflow:
    plan = TaskPlan(
        triggers=["form_submission"],
        actions=["post_to_slack"],
        notifications=["discord_webhook"],
        tools=["slack", "discord"],
    )
    return compile_workflow(plan, name="Good")


def test_valid_workflow_has_no_errors():
    issues = validate_workflow(_good())
    assert is_valid(issues)
    assert not [i for i in issues if i.severity == "error"]


def test_credential_report_is_emitted():
    issues = validate_workflow(_good())
    creds = [i for i in issues if i.code == "credential_required"]
    assert creds  # slack + discord need credentials


def test_no_trigger_is_error():
    wf = N8nWorkflow(name="X", nodes=[N8nNode(name="A", type="n8n-nodes-base.set", kind="action")])
    issues = validate_workflow(wf)
    assert not is_valid(issues)
    assert any(i.code == "no_trigger" for i in issues)


def test_multiple_triggers_is_error():
    wf = N8nWorkflow(
        name="X",
        nodes=[
            N8nNode(name="T1", type="n8n-nodes-base.webhook", kind="trigger"),
            N8nNode(name="T2", type="n8n-nodes-base.scheduleTrigger", kind="trigger"),
        ],
    )
    assert any(i.code == "multiple_triggers" for i in validate_workflow(wf))


def test_duplicate_names_is_error():
    wf = N8nWorkflow(
        name="X",
        nodes=[
            N8nNode(name="T", type="n8n-nodes-base.webhook", kind="trigger"),
            N8nNode(name="Dup", type="n8n-nodes-base.set", kind="action"),
            N8nNode(name="Dup", type="n8n-nodes-base.set", kind="action"),
        ],
    )
    assert any(i.code == "duplicate_node_name" for i in validate_workflow(wf))


def test_dangling_connection_target_is_error():
    wf = N8nWorkflow(
        name="X", nodes=[N8nNode(name="T", type="n8n-nodes-base.webhook", kind="trigger")]
    )
    wf.connect("T", "Ghost")  # target doesn't exist
    assert any(i.code == "dangling_target" for i in validate_workflow(wf))


def test_orphan_action_is_warning():
    wf = N8nWorkflow(
        name="X",
        nodes=[
            N8nNode(name="T", type="n8n-nodes-base.webhook", kind="trigger"),
            N8nNode(name="Island", type="n8n-nodes-base.set", kind="action"),  # never connected
        ],
    )
    issues = validate_workflow(wf)
    assert any(i.code == "orphan_node" for i in issues)
    assert is_valid(issues)  # orphan is a warning, not an error
