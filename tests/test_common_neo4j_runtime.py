"""Tests for backend.common.neo4j_runtime — exercise write_task_lineage."""
from __future__ import annotations

from typing import Any

from backend.common.neo4j_runtime import Neo4jRuntime


class _FakeClient:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.node_calls: list[tuple[str, dict[str, Any]]] = []
        self.rel_calls: list[tuple[str, Any, str, str, Any]] = []

    def write_node(self, label: str, properties: dict[str, Any]) -> bool:
        self.node_calls.append((label, dict(properties)))
        return True

    def write_relationship(
        self,
        source_label: str,
        source_key: Any,
        rel_type: str,
        target_label: str,
        target_key: Any,
    ) -> bool:
        self.rel_calls.append((source_label, source_key, rel_type, target_label, target_key))
        return True


def test_write_task_lineage_writes_task_audit_and_edge():
    fake = _FakeClient(available=True)
    rt = Neo4jRuntime(client=fake)

    ok = rt.write_task_lineage(
        service="leadgen",
        task_id="T-1",
        action="score",
        target_label=None,
        target_key=None,
        status="ok",
        audit_event_id="E-1",
    )

    assert ok is True
    # two node writes: Task then AuditEvent
    assert [c[0] for c in fake.node_calls] == ["Task", "AuditEvent"]

    task_props = fake.node_calls[0][1]
    assert task_props["task_id"] == "T-1"
    assert task_props["service"] == "leadgen"
    assert task_props["action"] == "score"
    assert task_props["status"] == "ok"
    assert "ts" in task_props

    audit_props = fake.node_calls[1][1]
    assert audit_props["event_id"] == "E-1"
    assert audit_props["task_id"] == "T-1"

    # one relationship: Task -[:EMITTED]-> AuditEvent
    assert fake.rel_calls == [("Task", "T-1", "EMITTED", "AuditEvent", "E-1")]


def test_write_task_lineage_adds_targeted_edge_when_target_given():
    fake = _FakeClient(available=True)
    rt = Neo4jRuntime(client=fake)

    rt.write_task_lineage(
        service="prospecting",
        task_id="T-2",
        action="discover",
        target_label="Prospect",
        target_key="P-9",
        status="ok",
        audit_event_id="E-2",
    )

    rels = fake.rel_calls
    assert ("Task", "T-2", "EMITTED", "AuditEvent", "E-2") in rels
    assert ("Task", "T-2", "TARGETED", "Prospect", "P-9") in rels
    assert len(rels) == 2


def test_write_task_lineage_noop_when_client_unavailable():
    fake = _FakeClient(available=False)
    rt = Neo4jRuntime(client=fake)

    ok = rt.write_task_lineage(
        service="leadgen",
        task_id="T-3",
        action="score",
        target_label=None,
        target_key=None,
        status="ok",
        audit_event_id="E-3",
    )

    assert ok is False
    assert fake.node_calls == []
    assert fake.rel_calls == []


def test_module_level_write_task_lineage_uses_default_runtime(monkeypatch):
    from backend.common import neo4j_runtime as rt_mod

    fake = _FakeClient(available=True)
    rt_mod.reset_runtime()
    monkeypatch.setattr(rt_mod, "_default_runtime", lambda: Neo4jRuntime(client=fake))

    ok = rt_mod.write_task_lineage(
        service="leadgen",
        task_id="T-mod",
        action="score",
        target_label=None,
        target_key=None,
        status="ok",
        audit_event_id="E-mod",
    )

    assert ok is True
    assert any(c[1].get("task_id") == "T-mod" for c in fake.node_calls)
