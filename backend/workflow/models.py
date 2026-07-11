"""n8n workflow JSON shapes (stdlib-only dataclasses).

``N8nWorkflow.to_dict()`` emits the exact structure n8n's *Import from File* and
``POST /api/v1/workflows`` accept: a ``nodes`` list and a ``connections`` map
keyed by the **source node name** (n8n wires by name, not id).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# n8n node "kind" for our own bookkeeping (not part of the n8n schema). Drives
# layout + validation (exactly one trigger, the error branch, etc.).
NodeKind = ("trigger", "action", "notification", "error_trigger")


@dataclass
class N8nNode:
    name: str                          # unique, human-readable; connections key off this
    type: str                          # e.g. "n8n-nodes-base.webhook"
    id: str = ""
    type_version: float = 1.0
    position: tuple[int, int] = (0, 0)
    parameters: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    # bookkeeping (not serialized into the node)
    kind: str = "action"
    credential_hint: str = ""          # human label of the credential to configure

    def to_dict(self) -> dict[str, Any]:
        node: dict[str, Any] = {
            "id": self.id or self.name.lower().replace(" ", "_"),
            "name": self.name,
            "type": self.type,
            "typeVersion": self.type_version,
            "position": [int(self.position[0]), int(self.position[1])],
            "parameters": self.parameters,
        }
        if self.credentials:
            node["credentials"] = self.credentials
        return node


@dataclass
class N8nWorkflow:
    name: str
    nodes: list[N8nNode] = field(default_factory=list)
    # source-name -> {"main": [[{"node": target, "type": "main", "index": 0}]]}
    connections: dict[str, dict[str, Any]] = field(default_factory=dict)
    active: bool = False
    settings: dict[str, Any] = field(default_factory=lambda: {"executionOrder": "v1"})

    def connect(self, source: str, target: str) -> None:
        """Wire ``source`` node's main output to ``target`` node's main input."""
        slot = self.connections.setdefault(source, {}).setdefault("main", [[]])
        slot[0].append({"node": target, "type": "main", "index": 0})

    def node_names(self) -> set[str]:
        return {n.name for n in self.nodes}

    def triggers(self) -> list[N8nNode]:
        return [n for n in self.nodes if n.kind == "trigger"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "nodes": [n.to_dict() for n in self.nodes],
            "connections": self.connections,
            "active": self.active,
            "settings": self.settings,
        }


__all__ = ["N8nNode", "N8nWorkflow", "NodeKind"]
