"""Deeper proposal coverage — pipeline edge cases, service branches, app endpoints."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.proposal.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


# ---------------------------------------------------------------------------
# pipeline edge cases
# ---------------------------------------------------------------------------


def test_compile_workflow_pushes_one_tool_per_template():
    from backend.proposal.models import (
        TaskPlan,
        TemplateDefinition,
        TemplateMaturity,
    )
    from backend.proposal.pipeline import compile_workflow

    templates = [
        TemplateDefinition(
            template_id="tr1",
            type="trigger",
            description="t1",
            supported_tools=["tool_x", "tool_y", "tool_z"],
            supported_triggers=["want_a"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
        TemplateDefinition(
            template_id="ac1",
            type="action",
            description="a1",
            supported_tools=["tool_p", "tool_q"],
            supported_actions=["want_b"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
        TemplateDefinition(
            template_id="nt1",
            type="notification",
            description="n1",
            supported_tools=["tool_m", "tool_n"],
            supported_notifications=["want_c"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
    ]
    plan = TaskPlan(triggers=["want_a"], actions=["want_b"], notifications=["want_c"])
    wf = compile_workflow(plan, templates)
    assert len(wf.used_tools) == 3
    assert wf.used_tools[0] in {"tool_x", "tool_y", "tool_z"}
    assert wf.used_tools[1] in {"tool_p", "tool_q"}
    assert wf.used_tools[2] in {"tool_m", "tool_n"}


def test_compile_workflow_chains_with_correct_edges():
    from backend.proposal.models import (
        TaskPlan,
        TemplateDefinition,
        TemplateMaturity,
    )
    from backend.proposal.pipeline import compile_workflow

    templates = [
        TemplateDefinition(
            template_id="tr1",
            type="trigger",
            description="t",
            supported_tools=["a"],
            supported_triggers=["w_tr"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
        TemplateDefinition(
            template_id="ac1",
            type="action",
            description="a1",
            supported_tools=["b"],
            supported_actions=["w_a1"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
        TemplateDefinition(
            template_id="ac2",
            type="action",
            description="a2",
            supported_tools=["c"],
            supported_actions=["w_a2"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
        TemplateDefinition(
            template_id="nt1",
            type="notification",
            description="n",
            supported_tools=["d"],
            supported_notifications=["w_n"],
            maturity=TemplateMaturity.PRODUCTION,
        ),
    ]
    plan = TaskPlan(triggers=["w_tr"], actions=["w_a1", "w_a2"], notifications=["w_n"])
    wf = compile_workflow(plan, templates)
    assert len(wf.nodes) == 4
    assert len(wf.edges) == 3
    edge_pairs = [(e.source_node_id, e.target_node_id) for e in wf.edges]
    assert ("trigger_0", "action_0") in edge_pairs
    assert ("action_0", "action_1") in edge_pairs
    assert ("action_1", "notify_0") in edge_pairs


def test_validate_workflow_empty_workflow():
    from backend.proposal.models import CompiledWorkflow
    from backend.proposal.pipeline import validate_workflow

    wf = CompiledWorkflow(nodes=[], edges=[], total_steps=0, used_tools=[])
    v = validate_workflow(wf)
    assert v.passes is False
    assert any("empty_workflow" in r for r in v.reasons)


def test_validate_workflow_passes_at_exact_limits():
    from backend.proposal.models import CompiledWorkflow, WorkflowNode
    from backend.proposal.pipeline import (
        MAX_EXTERNAL_TOOLS,
        MAX_TEMPLATES,
        MAX_WORKFLOW_STEPS,
        validate_workflow,
    )

    template_ids = ["t1", "t1", "t2", "t2", "t3"]
    nodes = [
        WorkflowNode(node_id=f"n{i}", kind="action", template_id=tid, description="x")
        for i, tid in enumerate(template_ids)
    ]
    wf = CompiledWorkflow(
        nodes=nodes,
        edges=[],
        total_steps=MAX_WORKFLOW_STEPS,
        used_tools=["a"] * MAX_EXTERNAL_TOOLS,
    )
    assert len({n.template_id for n in nodes}) == MAX_TEMPLATES
    v = validate_workflow(wf)
    assert v.passes is True, v.reasons
    assert v.reasons == []


# ---------------------------------------------------------------------------
# service branches
# ---------------------------------------------------------------------------


def test_service_needs_review_when_no_templates_match(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.proposal.models import (
        OnboardingIntake,
        PipelineStage,
        ProposalRequest,
    )
    from backend.proposal.service import generate_proposal

    result = generate_proposal(
        ProposalRequest(
            task_id="t-bogus",
            intake=OnboardingIntake(
                client_name="Acme",
                business_goal="unknown",
                triggers_wanted=["totally_bogus_trigger"],
                actions_wanted=["bogus_action"],
                notifications_wanted=["bogus_notify"],
            ),
        )
    )
    assert result.status == "needs_review"
    assert result.refund_protocol is False
    assert result.stage == PipelineStage.PENDING_INTAKE
    assert any("empty_workflow" in r for r in result.validation.reasons)


def test_service_idempotent_cache_hit(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.proposal.models import OnboardingIntake, ProposalRequest
    from backend.proposal.service import generate_proposal

    req = ProposalRequest(
        task_id="t-cache",
        intake=OnboardingIntake(
            client_name="Acme",
            business_goal="route leads",
            triggers_wanted=["form_submitted"],
            actions_wanted=["create_contact"],
            notifications_wanted=["slack_message"],
        ),
    )
    first = generate_proposal(req)
    second = generate_proposal(req)
    assert first.cache_hit is False
    assert second.cache_hit is True
    a = first.model_dump()
    b = second.model_dump()
    a.pop("cache_hit", None)
    b.pop("cache_hit", None)
    assert a == b


# ---------------------------------------------------------------------------
# app endpoints (TestClient)
# ---------------------------------------------------------------------------


def test_app_generate_endpoint_returns_proposal_result(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.proposal.app import app

    client = TestClient(app)
    r = client.post(
        "/generate",
        json={
            "task_id": "t-app-gen",
            "intake": {
                "client_name": "Acme",
                "business_goal": "leads to crm",
                "triggers_wanted": ["form_submitted"],
                "actions_wanted": ["create_contact"],
                "notifications_wanted": ["slack_message"],
                "tools_available": [],
                "budget_usd": None,
            },
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["workflow"]["total_steps"] == 3


def test_app_validate_endpoint_returns_validation(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.proposal.app import app

    client = TestClient(app)
    r = client.post(
        "/validate",
        json={
            "nodes": [],
            "edges": [],
            "total_steps": 0,
            "used_tools": [],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passes"] is False
    assert any("empty_workflow" in reason for reason in body["reasons"])


def test_app_work_endpoint_routes_by_action_metadata(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_PROPOSAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.proposal.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-work",
            "payload": {
                "task_id": "t-work",
                "intake": {
                    "client_name": "Acme",
                    "business_goal": "route leads",
                    "triggers_wanted": ["form_submitted"],
                    "actions_wanted": ["create_contact"],
                    "notifications_wanted": ["slack_message"],
                    "tools_available": [],
                    "budget_usd": None,
                },
            },
            "metadata": {"action": "generate_proposal"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["task_id"] == "t-work"
