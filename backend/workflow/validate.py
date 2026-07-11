"""Structural validation of a compiled n8n workflow.

Catches the mistakes that would make an import fail or a workflow silently do
nothing: no trigger / multiple main triggers, duplicate node names, connections
that reference a missing node, and orphan (unreachable) action nodes. Also emits
a non-blocking credential report so the runbook + operator know exactly what to
configure. ``is_valid`` is True when there are no ``error``-severity issues.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.workflow.models import N8nWorkflow


@dataclass
class Issue:
    severity: str  # error | warning | info
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


def validate_workflow(wf: N8nWorkflow) -> list[Issue]:
    issues: list[Issue] = []
    names = [n.name for n in wf.nodes]
    name_set = set(names)

    # exactly one main trigger (error triggers don't count)
    triggers = [n for n in wf.nodes if n.kind == "trigger"]
    if len(triggers) == 0:
        issues.append(Issue("error", "no_trigger", "workflow has no trigger node"))
    elif len(triggers) > 1:
        issues.append(
            Issue(
                "error",
                "multiple_triggers",
                f"workflow has {len(triggers)} triggers: {', '.join(t.name for t in triggers)}",
            )
        )

    # unique node names (connections key off names)
    if len(names) != len(name_set):
        dupes = sorted({n for n in names if names.count(n) > 1})
        issues.append(
            Issue("error", "duplicate_node_name", f"duplicate node names: {', '.join(dupes)}")
        )

    # connection integrity
    for source, conn in wf.connections.items():
        if source not in name_set:
            issues.append(
                Issue("error", "dangling_source", f"connection from unknown node '{source}'")
            )
        for branch in conn.get("main", []):
            for link in branch:
                tgt = link.get("node")
                if tgt not in name_set:
                    issues.append(
                        Issue(
                            "error",
                            "dangling_target",
                            f"connection to unknown node '{tgt}' (from '{source}')",
                        )
                    )

    # orphan detection: every action/notification reachable from the main trigger
    # (the error branch is intentionally separate and excluded).
    if triggers:
        reachable = _reachable(wf, triggers[0].name)
        error_branch = {n.name for n in wf.nodes if n.kind == "error_trigger"}
        error_branch |= _reachable(wf, *error_branch) if error_branch else set()
        for n in wf.nodes:
            if n.kind in ("trigger", "error_trigger"):
                continue
            if n.name not in reachable and n.name not in error_branch:
                issues.append(
                    Issue(
                        "warning",
                        "orphan_node",
                        f"node '{n.name}' is not reachable from the trigger",
                    )
                )

    # credential report (non-blocking)
    for n in wf.nodes:
        if n.credential_hint:
            issues.append(
                Issue(
                    "info",
                    "credential_required",
                    f"'{n.name}' needs credential: {n.credential_hint}",
                )
            )

    return issues


def _reachable(wf: N8nWorkflow, *roots: str) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for branch in wf.connections.get(cur, {}).get("main", []):
            for link in branch:
                tgt = link.get("node")
                if tgt and tgt not in seen:
                    stack.append(tgt)
    return seen


def is_valid(issues: list[Issue]) -> bool:
    return not any(i.severity == "error" for i in issues)


__all__ = ["Issue", "validate_workflow", "is_valid"]
