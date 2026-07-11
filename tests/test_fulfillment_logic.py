"""Tests for backend.fulfillment.logic."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.fulfillment.logic as logic_mod

    monkeypatch.setattr(logic_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    return fresh


def test_risk_score_from_level():
    from backend.fulfillment.logic import risk_score_from_level

    assert risk_score_from_level("normal") == 10
    assert risk_score_from_level("high") == 45
    assert risk_score_from_level("critical") == 80
    assert risk_score_from_level("unknown") == 10


def test_build_execution_graph_shape():
    from backend.fulfillment.logic import build_execution_graph

    g = build_execution_graph("ship it", [{"action": "step_a"}, {"action": "step_b"}])
    ids = [s["id"] for s in g]
    assert ids[0] == "validate_inputs"
    assert ids[1] == "prepare_assets"
    assert "action_1" in ids and "action_2" in ids
    assert ids[-1] == "verify_output"

    by_id = {s["id"]: s for s in g}
    assert by_id["prepare_assets"]["depends_on"] == ["validate_inputs"]
    assert by_id["action_1"]["depends_on"] == ["prepare_assets"]
    assert by_id["action_2"]["depends_on"] == ["action_1"]
    assert by_id["verify_output"]["depends_on"] == ["action_2"]


def test_build_execution_graph_empty_actions():
    from backend.fulfillment.logic import build_execution_graph

    g = build_execution_graph("do thing", [])
    ids = [s["id"] for s in g]
    assert "action_1" in ids
    assert ids[-1] == "verify_output"


def test_build_runbook_shape():
    from backend.fulfillment.logic import build_runbook

    rb = build_runbook("do thing", [{"action": "x"}])
    assert rb["objective"] == "do thing"
    assert "confirm inputs" in rb["prechecks"]
    assert "verify output" in rb["postchecks"]
    assert rb["execution"] == [{"action": "x"}]


def test_plan_fulfillment_normal_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.fulfillment.logic import plan_fulfillment

    result = plan_fulfillment(
        "task-1",
        payload={"objective": "tidy the inbox", "actions": [{"action": "sort"}]},
        metadata={"approvals": []},
    )
    assert result["task_id"] == "task-1"
    assert result["status"] in ("approved", "blocked")
    assert "execution_graph" in result and result["execution_graph"]
    assert "runbook" in result
    assert result["risk_assessment"]["risk_level"] in ("normal", "high", "critical")


def test_plan_fulfillment_cache_hit(tmp_path, monkeypatch):
    fresh = _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.fulfillment.logic import plan_fulfillment

    a = plan_fulfillment("task-x", payload={"objective": "noop"}, metadata={})
    b = plan_fulfillment("task-x", payload={"objective": "noop"}, metadata={})
    assert a == b
    assert fresh.exists("fulfillment:task-x")


def test_audit_path_default_is_container_relative():
    """The audit ledger default must be the container /opt/samus/data tree —
    a Windows E:\\ path is absent from the Cloud Run / Docker image and
    silently OSErrors on every append."""
    from backend.fulfillment import logic

    assert logic._AUDIT_PATH_DEFAULT == "/opt/samus/data/fulfillment/fulfillment_audit.jsonl"
    assert "\\" not in logic._AUDIT_PATH_DEFAULT


def test_audit_ledger_resolves_default_path_without_env(monkeypatch):
    """With SAMUS_FULFILLMENT_AUDIT_PATH unset the ledger is constructed
    against the container default (not the dead Windows path)."""
    monkeypatch.delenv("SAMUS_FULFILLMENT_AUDIT_PATH", raising=False)
    from backend.fulfillment import logic

    seen: list[str] = []

    class _StubLedger:
        def __init__(self, path):
            seen.append(str(path))

    monkeypatch.setattr(logic.persistence, "JsonlLedger", _StubLedger)
    logic._audit_ledger()
    assert seen == ["/opt/samus/data/fulfillment/fulfillment_audit.jsonl"]
