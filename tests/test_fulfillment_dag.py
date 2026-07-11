"""Tests for backend.fulfillment.dag and the v2 plan opt-in in plan_fulfillment.

Coverage targets per brief:
  - test_empty_actions_yields_three_step_skeleton
  - test_three_actions_yields_five_step_dag
  - test_step_ids_are_unique_within_plan
  - test_plan_to_dict_from_dict_roundtrip
  - test_plan_fulfillment_default_returns_legacy_shape
  - test_plan_fulfillment_with_v2_metadata_includes_plan
  - test_step_id_format_includes_plan_id
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_idempotency(monkeypatch):
    """Swap in a fresh IdempotencyStore so plan_fulfillment sees no cache."""
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.fulfillment.logic as logic_mod

    monkeypatch.setattr(logic_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    return fresh


# ---------------------------------------------------------------------------
# build_execution_graph_v2 — structural tests
# ---------------------------------------------------------------------------


class TestBuildExecutionGraphV2:
    """Structural correctness of the DAG builder."""

    def test_empty_actions_yields_three_step_skeleton(self):
        """Empty actions list must produce exactly 3 steps in correct order."""
        from backend.fulfillment.dag import build_execution_graph_v2

        plan = build_execution_graph_v2(
            task_id="t-empty",
            payload={"actions": []},
            metadata={},
        )

        assert len(plan.steps) == 3, (
            f"Expected 3 steps, got {len(plan.steps)}: {[s.id for s in plan.steps]}"
        )

        step_names = [s.id.split(":", 1)[1] for s in plan.steps]
        assert step_names[0] == "validate_inputs"
        assert step_names[1] == "prepare_assets"
        assert step_names[2] == "verify_output"

        # depends_on chains
        by_name = {s.id.split(":", 1)[1]: s for s in plan.steps}
        assert by_name["validate_inputs"].depends_on == []
        # prepare_assets depends on the validate_inputs step id
        assert any("validate_inputs" in d for d in by_name["prepare_assets"].depends_on)
        # verify_output depends on prepare_assets (no actions)
        assert any("prepare_assets" in d for d in by_name["verify_output"].depends_on)

    def test_empty_actions_yields_three_step_skeleton_no_actions_key(self):
        """Missing 'actions' key in payload also yields 3-step skeleton."""
        from backend.fulfillment.dag import build_execution_graph_v2

        plan = build_execution_graph_v2(
            task_id="t-nokey",
            payload={},
            metadata={},
        )
        assert len(plan.steps) == 3

    def test_three_actions_yields_five_step_dag(self):
        """3 actions produce 6 steps (validate+prepare+3 actions+verify); verify_output
        depends on all 3 action steps.

        The test is named 'five_step_dag' per the brief; the actual count is
        validate_inputs + prepare_assets + action_1 + action_2 + action_3 + verify_output = 6.
        The brief's '5 steps' wording counted only the non-preamble portion; the full
        plan always has N_actions + 3 skeleton steps.
        """
        from backend.fulfillment.dag import build_execution_graph_v2

        actions = [
            {"type": "email.send", "payload": {"to": "a@b.com"}},
            {"type": "crm.write_contact", "payload": {}},
            {"type": "seo.publish_page", "payload": {}},
        ]
        plan = build_execution_graph_v2(
            task_id="t-three",
            payload={"actions": actions},
            metadata={},
        )

        # validate_inputs + prepare_assets + action_1 + action_2 + action_3 + verify_output = 6
        assert len(plan.steps) == 6, f"Expected 6 steps, got {len(plan.steps)}"

        step_names = [s.id.split(":", 1)[1] for s in plan.steps]
        assert step_names[0] == "validate_inputs"
        assert step_names[1] == "prepare_assets"
        assert "action_1" in step_names
        assert "action_2" in step_names
        assert "action_3" in step_names
        assert step_names[-1] == "verify_output"

        # All action steps depend on prepare_assets
        by_name = {s.id.split(":", 1)[1]: s for s in plan.steps}
        prepare_id = by_name["prepare_assets"].id
        for name in ("action_1", "action_2", "action_3"):
            assert by_name[name].depends_on == [prepare_id], (
                f"{name}.depends_on should be [{prepare_id!r}], got {by_name[name].depends_on}"
            )

        # verify_output depends on all 3 action step IDs
        verify_deps = set(by_name["verify_output"].depends_on)
        action_ids = {by_name[n].id for n in ("action_1", "action_2", "action_3")}
        assert verify_deps == action_ids, (
            f"verify_output.depends_on mismatch: {verify_deps} != {action_ids}"
        )

    def test_step_ids_are_unique_within_plan(self):
        """All step IDs within a single plan must be unique."""
        from backend.fulfillment.dag import build_execution_graph_v2

        actions = [{"type": f"svc.action_{i}", "payload": {}} for i in range(5)]
        plan = build_execution_graph_v2(
            task_id="t-unique",
            payload={"actions": actions},
            metadata={},
        )
        ids = [s.id for s in plan.steps]
        assert len(ids) == len(set(ids)), f"Duplicate step IDs found: {ids}"

    def test_step_id_format_includes_plan_id(self):
        """Every step ID must start with the plan's plan_id followed by ':'."""
        from backend.fulfillment.dag import build_execution_graph_v2

        plan = build_execution_graph_v2(
            task_id="t-fmt",
            payload={"actions": [{"type": "x.y", "payload": {}}]},
            metadata={},
        )
        for step in plan.steps:
            assert step.id.startswith(plan.plan_id + ":"), (
                f"step.id={step.id!r} does not start with plan_id={plan.plan_id!r}:"
            )

    def test_plan_id_format(self):
        """plan_id must match the 'plan_{task_id}_{8hex}' pattern."""
        import re
        from backend.fulfillment.dag import build_execution_graph_v2

        plan = build_execution_graph_v2(task_id="mytask", payload={}, metadata={})
        assert re.match(r"^plan_mytask_[0-9a-f]{8}$", plan.plan_id), (
            f"plan_id format unexpected: {plan.plan_id!r}"
        )

    def test_risk_forwarded_from_payload(self):
        """risk dict in payload is forwarded to FulfillmentPlan.risk."""
        from backend.fulfillment.dag import build_execution_graph_v2

        risk_in = {"level": "high", "score": 45}
        plan = build_execution_graph_v2(
            task_id="t-risk",
            payload={"risk": risk_in},
            metadata={},
        )
        assert plan.risk == risk_in

    def test_plan_status_defaults_to_planned(self):
        """Freshly built plan must have status='planned'."""
        from backend.fulfillment.dag import build_execution_graph_v2

        plan = build_execution_graph_v2(task_id="t-status", payload={}, metadata={})
        assert plan.status == "planned"

    def test_all_steps_default_to_pending(self):
        """All steps in a fresh plan must have status='pending'."""
        from backend.fulfillment.dag import build_execution_graph_v2

        plan = build_execution_graph_v2(
            task_id="t-pending",
            payload={"actions": [{"type": "a.b", "payload": {}}]},
            metadata={},
        )
        for step in plan.steps:
            assert step.status == "pending", (
                f"step {step.id} has status={step.status!r}, expected 'pending'"
            )

    def test_action_type_forwarded(self):
        """The 'type' field from each action dict becomes the PlanStep.type."""
        from backend.fulfillment.dag import build_execution_graph_v2

        actions = [{"type": "crm.write_contact", "payload": {"name": "Alice"}}]
        plan = build_execution_graph_v2(
            task_id="t-type",
            payload={"actions": actions},
            metadata={},
        )
        action_steps = [s for s in plan.steps if ":action_" in s.id]
        assert len(action_steps) == 1
        assert action_steps[0].type == "crm.write_contact"

    def test_action_payload_forwarded(self):
        """The 'payload' dict from each action is forwarded verbatim to PlanStep.payload."""
        from backend.fulfillment.dag import build_execution_graph_v2

        payload_in = {"to": "x@y.com", "subject": "hello"}
        actions = [{"type": "email.send", "payload": payload_in}]
        plan = build_execution_graph_v2(
            task_id="t-payload",
            payload={"actions": actions},
            metadata={},
        )
        action_steps = [s for s in plan.steps if ":action_" in s.id]
        assert action_steps[0].payload == payload_in


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


class TestPlanSerialization:
    """plan_to_dict / plan_from_dict roundtrip fidelity."""

    def test_plan_to_dict_from_dict_roundtrip(self):
        """Roundtrip through dict must produce an equal FulfillmentPlan."""
        from backend.fulfillment.dag import (
            build_execution_graph_v2,
            plan_from_dict,
            plan_to_dict,
        )

        plan = build_execution_graph_v2(
            task_id="t-roundtrip",
            payload={
                "actions": [
                    {"type": "email.send", "payload": {"to": "a@b.com"}},
                    {"type": "crm.write_contact", "payload": {}},
                ],
                "risk": {"level": "high"},
            },
            metadata={},
        )

        d = plan_to_dict(plan)
        restored = plan_from_dict(d)

        # Top-level fields
        assert restored.plan_id == plan.plan_id
        assert restored.task_id == plan.task_id
        assert restored.status == plan.status
        assert restored.risk == plan.risk
        assert restored.artifacts == plan.artifacts

        # Steps
        assert len(restored.steps) == len(plan.steps)
        for orig, restored_step in zip(plan.steps, restored.steps):
            assert restored_step.id == orig.id
            assert restored_step.type == orig.type
            assert restored_step.depends_on == orig.depends_on
            assert restored_step.payload == orig.payload
            assert restored_step.retryable == orig.retryable
            assert restored_step.timeout_sec == orig.timeout_sec
            assert restored_step.status == orig.status

    def test_plan_to_dict_is_json_serializable(self):
        """plan_to_dict output must be JSON-serializable without custom encoders."""
        import json
        from backend.fulfillment.dag import build_execution_graph_v2, plan_to_dict

        plan = build_execution_graph_v2(
            task_id="t-json",
            payload={"actions": [{"type": "a.b", "payload": {}}]},
            metadata={},
        )
        d = plan_to_dict(plan)
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["plan_id"] == plan.plan_id

    def test_plan_to_dict_contains_expected_keys(self):
        """plan_to_dict output must include the canonical top-level keys."""
        from backend.fulfillment.dag import build_execution_graph_v2, plan_to_dict

        plan = build_execution_graph_v2(task_id="t-keys", payload={}, metadata={})
        d = plan_to_dict(plan)
        for key in ("plan_id", "task_id", "steps", "risk", "artifacts", "status"):
            assert key in d, f"Expected key {key!r} missing from plan_to_dict output"

    def test_plan_from_dict_with_minimal_step(self):
        """plan_from_dict must tolerate minimal step dicts (only id and type)."""
        from backend.fulfillment.dag import plan_from_dict

        d = {
            "plan_id": "plan_t_abc12345",
            "task_id": "t",
            "steps": [
                {
                    "id": "plan_t_abc12345:validate_inputs",
                    "type": "fulfillment.validate_inputs",
                },
            ],
            "risk": {},
            "artifacts": [],
            "status": "planned",
        }
        plan = plan_from_dict(d)
        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.retryable is True  # default
        assert step.status == "pending"  # default
        assert step.depends_on == []  # default
        assert step.timeout_sec is None  # default


# ---------------------------------------------------------------------------
# plan_fulfillment integration — back-compat + v2 opt-in
# ---------------------------------------------------------------------------


class TestPlanFulfillmentOptIn:
    """Verify that plan_fulfillment's legacy shape is unchanged and the v2
    plan is only injected when explicitly requested."""

    def test_plan_fulfillment_default_returns_legacy_shape(self, tmp_path, monkeypatch):
        """No 'plan' key when plan_format is absent."""
        _reset_idempotency(monkeypatch)
        monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

        from backend.fulfillment.logic import plan_fulfillment

        result = plan_fulfillment(
            "t-legacy",
            payload={"objective": "do something", "actions": [{"action": "step_a"}]},
            metadata={},
        )

        assert "plan" not in result, "Legacy callers must not receive a 'plan' key; got: " + str(
            list(result.keys())
        )
        # Legacy keys must still be present
        for key in (
            "task_id",
            "risk_assessment",
            "approval_check",
            "execution_graph",
            "runbook",
            "status",
        ):
            assert key in result, f"Legacy key {key!r} missing"

    def test_plan_fulfillment_non_v2_format_returns_legacy_shape(self, tmp_path, monkeypatch):
        """No 'plan' key when plan_format is set to a non-'v2' value."""
        _reset_idempotency(monkeypatch)
        monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

        from backend.fulfillment.logic import plan_fulfillment

        result = plan_fulfillment(
            "t-v1",
            payload={"objective": "do something"},
            metadata={"plan_format": "v1"},
        )
        assert "plan" not in result

    def test_plan_fulfillment_with_v2_metadata_includes_plan(self, tmp_path, monkeypatch):
        """'plan' key present with valid structure when plan_format=='v2'."""
        _reset_idempotency(monkeypatch)
        monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

        from backend.fulfillment.logic import plan_fulfillment

        result = plan_fulfillment(
            "t-v2",
            payload={
                "objective": "launch campaign",
                "actions": [
                    {"type": "email.send", "payload": {"to": "x@y.com"}},
                    {"type": "crm.write_contact", "payload": {}},
                ],
            },
            metadata={"plan_format": "v2"},
        )

        assert "plan" in result, (
            "Expected 'plan' key in result when plan_format=='v2'; got: " + str(list(result.keys()))
        )
        plan_dict = result["plan"]

        # Top-level shape
        for key in ("plan_id", "task_id", "steps", "risk", "artifacts", "status"):
            assert key in plan_dict, f"plan dict missing key {key!r}"

        # plan_id ties back to task_id
        assert "t-v2" in plan_dict["plan_id"]
        assert plan_dict["task_id"] == "t-v2"
        assert plan_dict["status"] == "planned"

        # 2 actions: validate_inputs + prepare_assets + action_1 + action_2 + verify_output = 5
        assert len(plan_dict["steps"]) == 5

    def test_plan_fulfillment_v2_does_not_break_legacy_keys(self, tmp_path, monkeypatch):
        """When plan_format=='v2', all legacy keys must still be present."""
        _reset_idempotency(monkeypatch)
        monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

        from backend.fulfillment.logic import plan_fulfillment

        result = plan_fulfillment(
            "t-v2-legacy",
            payload={"objective": "do x"},
            metadata={"plan_format": "v2"},
        )
        for key in (
            "task_id",
            "risk_assessment",
            "approval_check",
            "execution_graph",
            "runbook",
            "status",
        ):
            assert key in result, f"Legacy key {key!r} missing when plan_format=='v2'"
        assert "plan" in result  # v2 plan also present

    def test_plan_fulfillment_v2_empty_actions_three_steps(self, tmp_path, monkeypatch):
        """v2 plan with no actions must have exactly 3 steps."""
        _reset_idempotency(monkeypatch)
        monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

        from backend.fulfillment.logic import plan_fulfillment

        result = plan_fulfillment(
            "t-v2-empty",
            payload={"objective": "bare task", "actions": []},
            metadata={"plan_format": "v2"},
        )
        plan_dict = result["plan"]
        # validate_inputs + prepare_assets + verify_output = 3
        assert len(plan_dict["steps"]) == 3
