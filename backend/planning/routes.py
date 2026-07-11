"""Gateway route registration for the planning + explainability surfaces.

Defined here (an owned Tranche-4 package) so ``gateway/app.py`` gains only a
single registration line — sibling branches edit that file too. Registers:

  * ``GET  /autonomy/plan``      — inspect the current goal tree + active plans
                                   (the existing POST form stays in app.py).
  * ``GET  /admin/decisions``    — the decision log (list + drill-down) from the
                                   unified stream.
  * ``GET  /admin/decisions/{id}`` — one DecisionRecord (why/alternatives/data).
  * ``GET  /admin/approvals``    — pending operator approvals (T1 store; the
                                   route wiring was deferred, landed here since
                                   the command center consumes it).
  * ``POST /admin/approvals/decide`` — approve/reject one request.
  * ``GET  /admin/command_center`` — the single executive aggregate.

Every route follows the existing admin pattern: a top-of-handler
``check_capability("gateway", <cap>)`` gate, best-effort body, and a
never-500 degrade (an operator surface returns ``ok=False`` + an error string
rather than raising).
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common.capabilities import check_capability

_LOG = logging.getLogger("samus.planning.routes")


def register_planning_routes(app: Any) -> None:
    """Attach the planning + explainability + command-center routes."""

    # -- GET /autonomy/plan (inspect) ----------------------------------------
    @app.get("/autonomy/plan")
    async def autonomy_plan_get() -> dict[str, Any]:
        """Inspect the current goal tree + active plans.

        Read-only companion to the existing ``POST /autonomy/plan`` (which runs
        a MAPE-K cycle). Capability-gated (``autonomy_plan``). Never 500s.
        """
        check_capability("gateway", "autonomy_plan")
        try:
            from backend.planning.planner import current_plans_view

            return current_plans_view()
        except Exception as exc:  # noqa: BLE001 — operator surface never 500s
            _LOG.warning("GET /autonomy/plan failed: %s", exc)
            return {"ok": False, "error": str(exc), "goals": [], "active_plans": []}

    # -- GET /admin/decisions (list) -----------------------------------------
    @app.get("/admin/decisions")
    async def admin_decisions(
        actor: str | None = None,
        prospect_id: str | None = None,
        opportunity_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Recent decision records from the unified stream, newest first.

        Answers why/alternatives/data/expected-outcome for every autonomous
        decision. Filterable by ``actor`` / ``prospect_id`` / ``opportunity_id``.
        Capability-gated (``journey_read``).
        """
        check_capability("gateway", "journey_read")
        try:
            from backend.common.decision_record import list_decisions

            rows = list_decisions(
                actor=actor,
                prospect_id=prospect_id,
                opportunity_id=opportunity_id,
                limit=limit,
            )
            return {"ok": True, "decisions": rows, "count": len(rows)}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("GET /admin/decisions failed: %s", exc)
            return {"ok": False, "error": str(exc), "decisions": [], "count": 0}

    # -- GET /admin/decisions/{decision_id} (drill-down) ---------------------
    @app.get("/admin/decisions/{decision_id}")
    async def admin_decision_detail(decision_id: str) -> dict[str, Any]:
        """One decision record by id (the four-questions drill-down).

        Capability-gated (``journey_read``). Returns ``ok=False`` on unknown id
        rather than 404 so a dashboard needn't special-case a telemetry gap.
        """
        check_capability("gateway", "journey_read")
        try:
            from backend.common.decision_record import get_decision

            rec = get_decision(decision_id)
            if rec is None:
                return {"ok": False, "error": "not_found", "decision": None}
            return {"ok": True, "decision": rec}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("GET /admin/decisions/%s failed: %s", decision_id, exc)
            return {"ok": False, "error": str(exc), "decision": None}

    # -- GET /admin/approvals (pending queue) --------------------------------
    @app.get("/admin/approvals")
    async def admin_approvals(
        status: str = "pending", kind: str | None = None, limit: int = 200,
    ) -> dict[str, Any]:
        """Operator approval queue (T1 store). Defaults to pending.

        Capability-gated (``budget_admin`` — approvals are economic/risk gates).
        Auto-expires stale pending rows on read (fail-closed).
        """
        check_capability("gateway", "budget_admin")
        try:
            from backend.common.approvals import list_approvals

            rows = list_approvals(
                status=(status or None), kind=kind, limit=limit,
            )
            return {"ok": True, "approvals": rows, "count": len(rows)}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("GET /admin/approvals failed: %s", exc)
            return {"ok": False, "error": str(exc), "approvals": [], "count": 0}

    # -- POST /admin/approvals/decide ----------------------------------------
    @app.post("/admin/approvals/decide")
    async def admin_approvals_decide(payload: dict[str, Any]) -> dict[str, Any]:
        """Approve/reject one pending request, or batch-approve low-risk ones.

        Body: ``{"approval_id": str, "decision": "approved"|"rejected"}`` for a
        single decision, OR ``{"approval_ids": [str, ...]}`` to batch-approve
        (routine/low-risk only — emergency-severity requests are skipped and
        must be decided per-item). Capability-gated (``budget_admin``).
        """
        check_capability("gateway", "budget_admin")
        try:
            from backend.common.approvals import batch_approve, decide_approval

            if not isinstance(payload, dict):
                return {"ok": False, "error": "expected_json_object"}
            decided_by = str(payload.get("decided_by") or "operator")
            ids = payload.get("approval_ids")
            if isinstance(ids, list) and ids:
                result = batch_approve([str(i) for i in ids], decided_by=decided_by)
                return {"ok": True, "batch": result}
            approval_id = str(payload.get("approval_id") or "")
            decision = str(payload.get("decision") or "")
            if not approval_id or not decision:
                return {"ok": False, "error": "approval_id_and_decision_required"}
            row = decide_approval(approval_id, decision, decided_by=decided_by)
            if row is None:
                return {"ok": False, "error": "not_pending_or_unknown", "approval": None}
            return {"ok": True, "approval": row}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("POST /admin/approvals/decide failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    # -- GET /admin/command_center (the executive aggregate) -----------------
    @app.get("/admin/command_center")
    async def admin_command_center(
        prospect_id: str | None = None, day: str | None = None,
    ) -> dict[str, Any]:
        """The single executive aggregate: what happened / why / what's running
        now / what needs approval / economics / health.

        Reuses every existing admin surface's service functions (no
        reimplementation). Capability-gated (``journey_read``). Never 500s —
        each section degrades to an error note independently.
        """
        check_capability("gateway", "journey_read")
        try:
            from backend.planning.command_center import build_command_center

            return build_command_center(prospect_id=prospect_id, day=day)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("GET /admin/command_center failed: %s", exc)
            return {"ok": False, "error": str(exc)}


__all__ = ["register_planning_routes"]
