"""Execution-planning logic for the fulfillment workcell (doc §7).

plan_fulfillment composes: governance decision -> execution graph -> runbook ->
risk score. Idempotency keyed on ``f"fulfillment:{task_id}"``.

The governance import is defensive: this workcell expects the new
``approval_decision(objective, actions, approvals)`` API from
``backend.common.governance``. While the main session is still landing that
shape, we fall back to a conservative local decision so the rest of the
pipeline can be exercised end-to-end.

v2 plan opt-in:
    When ``metadata.get("plan_format") == "v2"`` the return dict gains a
    ``plan`` key whose value is the JSON-safe serialisation of a
    ``FulfillmentPlan`` built by ``build_execution_graph_v2``.  All other
    callers receive the legacy shape unchanged — no ``plan`` key is added.
"""
from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any

from backend.common import events, governance, persistence
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

_LOG = logging.getLogger("samus.fulfillment.logic")

# Container-relative ledger path: the prior Windows path (E:\Hustleforge\...)
# is absent from the Cloud Run / Docker image, so JsonlLedger.append silently
# OSErrored every run. /opt/samus/data/<workcell>/ matches crm/intake/proposal/
# seo and is bind-mounted in the compose stack. SAMUS_FULFILLMENT_AUDIT_PATH
# still overrides for host runs.
_AUDIT_PATH_DEFAULT = "/opt/samus/data/fulfillment/fulfillment_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_FULFILLMENT_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


# --- governance shim ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _LocalApprovalDecision:
    approved: bool
    risk_level: str
    reasons: tuple[str, ...]
    required_approvals: tuple[str, ...]


_RISK_KEYWORDS = {
    "critical": ("delete production", "drop table", "wipe", "exfiltrate"),
    "high": ("send email", "transfer funds", "publish", "deploy", "purchase"),
}


def _local_classify_risk(objective: str, actions: list[dict[str, Any]]) -> tuple[str, list[str]]:
    blob = " ".join(
        [objective or ""]
        + [str(a.get("action") or a.get("type") or a) for a in (actions or [])]
    ).lower()
    reasons: list[str] = []
    for kw in _RISK_KEYWORDS["critical"]:
        if kw in blob:
            reasons.append(f"critical_keyword:{kw}")
            return "critical", reasons
    for kw in _RISK_KEYWORDS["high"]:
        if kw in blob:
            reasons.append(f"high_keyword:{kw}")
            return "high", reasons
    if len(actions or []) > 20:
        reasons.append("bulk_batch")
        return "high", reasons
    reasons.append("no_risk_signal")
    return "normal", reasons


def _local_required_approvals(level: str) -> tuple[str, ...]:
    if level == "critical":
        return ("major", "human")
    if level == "high":
        return ("major",)
    return ()


def _call_approval_decision(
    objective: str,
    actions: list[dict[str, Any]],
    approvals: list[str],
) -> Any:
    """Try the new governance API; fall back to a local decision shim."""
    fn = getattr(governance, "approval_decision", None)
    if fn is not None:
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            if len(params) >= 3:
                return fn(objective, actions, approvals)
        except (TypeError, ValueError):
            pass
    # Fallback: use the local classifier so the rest of the pipeline keeps
    # working until the main session lands the new shape.
    level, reasons = _local_classify_risk(objective, actions)
    required = _local_required_approvals(level)
    have = set(approvals or [])
    missing = [a for a in required if a not in have]
    if missing:
        reasons.append("missing_approvals:" + ",".join(missing))
        return _LocalApprovalDecision(
            approved=False,
            risk_level=level,
            reasons=tuple(reasons),
            required_approvals=required,
        )
    return _LocalApprovalDecision(
        approved=True,
        risk_level=level,
        reasons=tuple(reasons),
        required_approvals=required,
    )


# --- planning primitives --------------------------------------------------

def risk_score_from_level(level: str) -> int:
    """Map a risk level string to a numeric score."""
    return {"normal": 10, "high": 45, "critical": 80}.get((level or "").lower(), 10)


def build_execution_graph(objective: str, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a topologically-ordered execution graph."""
    graph: list[dict[str, Any]] = [
        {
            "id": "validate_inputs",
            "blocking": True,
            "depends_on": [],
            "description": f"Validate inputs for: {objective}",
        },
        {
            "id": "prepare_assets",
            "blocking": True,
            "depends_on": ["validate_inputs"],
            "description": "Stage assets, credentials, and prechecks.",
        },
    ]
    actions_list = list(actions or [])
    if not actions_list:
        actions_list = [{"action": "execute objective"}]

    prev_id = "prepare_assets"
    last_action_id = prev_id
    for idx, action in enumerate(actions_list, start=1):
        action_id = f"action_{idx}"
        graph.append({
            "id": action_id,
            "blocking": False,
            "depends_on": [prev_id],
            "description": str(
                action.get("description")
                or action.get("action")
                or action.get("type")
                or f"action {idx}"
            ),
            "action": action,
        })
        prev_id = action_id
        last_action_id = action_id

    graph.append({
        "id": "verify_output",
        "blocking": True,
        "depends_on": [last_action_id],
        "description": "Verify outputs against acceptance criteria.",
    })
    return graph


def build_runbook(objective: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "objective": objective,
        "prechecks": [
            "confirm inputs",
            "confirm approvals",
            "confirm environment",
        ],
        "execution": list(actions or []),
        "postchecks": [
            "verify output",
            "log audit event",
            "persist result",
        ],
    }


def plan_fulfillment(
    task_id: str,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """End-to-end execution planning."""
    key = f"fulfillment:{task_id}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("plan_fulfillment cache hit task_id=%s", task_id)
        return cached

    objective = str(payload.get("objective") or "execute task")
    actions = list(payload.get("actions") or [])
    approvals = list((metadata or {}).get("approvals") or [])

    decision = _call_approval_decision(objective, actions, approvals)
    risk_level = getattr(decision, "risk_level", "normal")
    reasons = list(getattr(decision, "reasons", []) or [])
    required_approvals = list(getattr(decision, "required_approvals", []) or [])
    approved = bool(getattr(decision, "approved", True))

    graph = build_execution_graph(objective, actions)
    runbook = build_runbook(objective, actions)
    risk_score = risk_score_from_level(risk_level)

    result: dict[str, Any] = {
        "task_id": task_id,
        "risk_assessment": {
            "risk_level": risk_level,
            "risk_score": risk_score,
            "reasons": reasons,
        },
        "approval_check": {
            "approved": approved,
            "required_approvals": required_approvals,
            "reasons": reasons,
        },
        "execution_graph": graph,
        "runbook": runbook,
        "status": "approved" if approved else "blocked",
        "block_reason": (reasons[-1] if (not approved and reasons) else None),
    }

    # --- v2 structured plan (opt-in; legacy callers are unaffected) ----------
    if (metadata or {}).get("plan_format") == "v2":
        from .dag import build_execution_graph_v2, plan_to_dict  # lazy import avoids circular dep risk

        try:
            v2_plan = build_execution_graph_v2(task_id, payload, metadata)
            result["plan"] = plan_to_dict(v2_plan)
            _LOG.debug("v2 plan attached plan_id=%s task_id=%s", v2_plan.plan_id, task_id)
        except Exception as exc:  # pragma: no cover
            _LOG.warning("v2 plan build failed task_id=%s: %s", task_id, exc)

    GLOBAL_IDEMPOTENCY_STORE.set(key, result)

    audit_event = events.build_audit_event(
        service="fulfillment",
        task_id=task_id,
        action="plan_execution",
        input_payload={"objective": objective, "actions": actions, "metadata": metadata},
        output_payload={
            "status": result["status"],
            "risk_level": risk_level,
            "risk_score": risk_score,
        },
        status="completed" if approved else "blocked",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("fulfillment audit ledger append failed: %s", exc)

    return result
