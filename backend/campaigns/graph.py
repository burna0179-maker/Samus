"""Campaign graph — traversal over a validated :class:`CampaignTemplate`.

Wraps a template's nodes + edges as a directed acyclic graph and answers the
questions the orchestrator asks each tick:

* what are the *root* nodes (no incoming edge)?
* given the set of completed nodes, which nodes are now *ready* (all incoming
  edges satisfied, including any conditional branch predicate)?
* is the graph acyclic (a cycle would wedge the run)?

Conditional branching is expressed as an edge ``condition`` mapping; it is
evaluated by :func:`evaluate_condition` against the live run context using a
small, closed operator set — never ``eval``/arbitrary code, so a template
author cannot smuggle execution into a config file.
"""
from __future__ import annotations

from typing import Any

from .models import CampaignEdge, CampaignNode, CampaignTemplate


class GraphError(ValueError):
    """Raised for a structurally invalid graph (e.g. a cycle)."""


# Closed set of comparison operators a conditional edge may use.
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) > _num(b),
    "gte": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "lte": lambda a, b: _num(a) <= _num(b),
    "in": lambda a, b: a in b if isinstance(b, (list, tuple, set, str)) else False,
    "truthy": lambda a, _b: bool(a),
    "falsy": lambda a, _b: not bool(a),
}


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def evaluate_condition(condition: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    """Evaluate a conditional-edge predicate against ``context``.

    Shape: ``{"field": "kpi.application_starts", "op": "gte", "value": 10}``.
    A dotted ``field`` walks nested dicts (``kpi.<key>`` reads
    ``context["kpi"]["<key>"]``). ``None`` (no condition) is always True.
    Unknown operators / missing fields evaluate False (fail-closed — an
    ambiguous branch is not taken).
    """
    if not condition:
        return True
    op_name = str(condition.get("op", "truthy"))
    op = _OPS.get(op_name)
    if op is None:
        return False
    field = condition.get("field", "")
    left = _resolve_field(field, context)
    right = condition.get("value")
    try:
        return bool(op(left, right))
    except Exception:  # noqa: BLE001 — a broken predicate never crashes the run
        return False


def _resolve_field(field: str, context: dict[str, Any]) -> Any:
    cur: Any = context
    for part in str(field).split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class CampaignGraph:
    """A traversable view over a template's node/edge set."""

    def __init__(self, template: CampaignTemplate) -> None:
        self.template = template
        self.nodes: dict[str, CampaignNode] = template.node_map()
        self.edges: list[CampaignEdge] = list(template.edges)
        self._incoming: dict[str, list[CampaignEdge]] = {nid: [] for nid in self.nodes}
        self._outgoing: dict[str, list[CampaignEdge]] = {nid: [] for nid in self.nodes}
        for edge in self.edges:
            self._outgoing[edge.from_node].append(edge)
            self._incoming[edge.to_node].append(edge)
        self.assert_acyclic()

    # -- structure -------------------------------------------------------

    def roots(self) -> list[str]:
        """Node ids with no incoming edge, in template declaration order."""
        return [nid for nid in self.nodes if not self._incoming[nid]]

    def successors(self, node_id: str) -> list[CampaignEdge]:
        return list(self._outgoing.get(node_id, []))

    def predecessors(self, node_id: str) -> list[CampaignEdge]:
        return list(self._incoming.get(node_id, []))

    def assert_acyclic(self) -> None:
        """Raise :class:`GraphError` if the graph contains a cycle."""
        self.topological_order()  # raises on cycle

    def topological_order(self) -> list[str]:
        """Kahn's algorithm; raises :class:`GraphError` on a cycle."""
        indeg = {nid: len(self._incoming[nid]) for nid in self.nodes}
        queue = [nid for nid in self.nodes if indeg[nid] == 0]
        order: list[str] = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for edge in self._outgoing[nid]:
                indeg[edge.to_node] -= 1
                if indeg[edge.to_node] == 0:
                    queue.append(edge.to_node)
        if len(order) != len(self.nodes):
            remaining = [nid for nid in self.nodes if nid not in set(order)]
            raise GraphError(f"cycle detected among nodes: {sorted(remaining)}")
        return order

    # -- runtime traversal ----------------------------------------------

    def ready_nodes(
        self,
        completed: set[str],
        *,
        context: dict[str, Any] | None = None,
        exclude: set[str] | None = None,
    ) -> list[str]:
        """Nodes whose every incoming edge is satisfied and not yet run.

        An incoming edge is satisfied when its ``from_node`` is completed AND
        its (optional) condition evaluates true. A node with no incoming edge
        (a root) is ready immediately. ``exclude`` drops nodes already
        completed / failed / awaiting approval so they are not re-scheduled.
        """
        ctx = context or {}
        skip = set(completed) | set(exclude or set())
        ready: list[str] = []
        for nid in self.nodes:  # declaration order = deterministic scheduling
            if nid in skip:
                continue
            incoming = self._incoming[nid]
            if not incoming:
                ready.append(nid)
                continue
            if all(
                (edge.from_node in completed)
                and evaluate_condition(edge.condition, ctx)
                for edge in incoming
            ):
                ready.append(nid)
        return ready
