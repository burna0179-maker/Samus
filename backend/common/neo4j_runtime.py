"""High-level Neo4j helpers built on top of :class:`GraphClient`.

Per doc Section 10 (Neo4j Knowledge Graph Layer). Encodes the common write
patterns the rest of the agent reaches for so callers don't have to hand-roll
sequences of write_node + write_relationship calls.
"""
from __future__ import annotations

import logging
import threading

from .dates import iso_now
from .graph_client import GraphClient, get_client

_LOG = logging.getLogger("samus.common.neo4j_runtime")


class Neo4jRuntime:
    """Convenience wrapper for the common write paths.

    Methods become no-ops when the underlying :class:`GraphClient` reports
    ``available is False`` (e.g. Neo4j down + ``neo4j_required=False``).
    """

    def __init__(self, client: GraphClient | None = None) -> None:
        self.client = client or get_client()

    def write_task_lineage(
        self,
        *,
        service: str,
        task_id: str,
        action: str,
        target_label: str | None,
        target_key: str | None,
        status: str,
        audit_event_id: str,
    ) -> bool:
        """Ensure (Task)-[:EMITTED]->(AuditEvent) and optionally (Task)-[:TARGETED]->target.

        Returns True if writes were issued, False on no-op (graph unavailable).
        """
        if not self.client.available:
            return False
        ts = iso_now()
        self.client.write_node(
            "Task",
            {
                "task_id": task_id,
                "service": service,
                "action": action,
                "status": status,
                "ts": ts,
            },
        )
        self.client.write_node(
            "AuditEvent",
            {
                "event_id": audit_event_id,
                "service": service,
                "task_id": task_id,
                "action": action,
                "status": status,
                "ts": ts,
            },
        )
        self.client.write_relationship(
            "Task", task_id, "EMITTED", "AuditEvent", audit_event_id
        )
        if target_label and target_key:
            self.client.write_relationship(
                "Task", task_id, "TARGETED", target_label, target_key
            )
        return True


# --- module-level convenience ----------------------------------------------

_RUNTIME: Neo4jRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def _default_runtime() -> Neo4jRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = Neo4jRuntime()
    return _RUNTIME


def reset_runtime() -> None:
    """Test helper: drop the module-level Neo4jRuntime singleton."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = None


def write_task_lineage(
    *,
    service: str,
    task_id: str,
    action: str,
    target_label: str | None,
    target_key: str | None,
    status: str,
    audit_event_id: str,
) -> bool:
    """Module-level shortcut that delegates to the default Neo4jRuntime instance."""
    return _default_runtime().write_task_lineage(
        service=service,
        task_id=task_id,
        action=action,
        target_label=target_label,
        target_key=target_key,
        status=status,
        audit_event_id=audit_event_id,
    )
