"""Executive command center — the single operator aggregate.

HOTL Tranche 4 (framework Phase 4). ONE function, :func:`build_command_center`,
composes the six executive questions from the EXISTING admin surfaces' service
functions (no reimplementation):

    what_happened   — event/journey digest via business_events.read_events
    why             — recent DecisionRecords via decision_record.list_decisions
    running_now     — active plans (planner) + in-flight tasks (task_state) +
                      the latest control tick (control_tick_ledger)
    needs_approval  — pending approvals via approvals.list_approvals
    economics       — latest ROI roll-up via finance.roi.get_rollup
    health          — 4-state health (common.health) + latest entropy snapshot

This is the ONE source of truth: both ``GET /admin/command_center`` and the
morning brief's command-center summary render from this same dict.

Every section is independently best-effort — a broken source degrades to an
``error`` note on THAT section, never a raise. The top-level ``ok`` is True
unless the whole build fails.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import Counter
from typing import Any

from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.planning.command_center")


def _section_what_happened(prospect_id: str | None, since: str) -> dict[str, Any]:
    """Event digest: counts by type + the most recent events, from the stream."""
    try:
        from backend.common.business_events import read_events

        events = read_events(
            prospect_id=prospect_id, since=since, limit=2000,
        )
        by_type: Counter = Counter(str(e.get("event_type") or "") for e in events)
        recent = list(reversed(events))[:20]  # newest first
        return {
            "event_count": len(events),
            "by_type": dict(by_type),
            "recent": [
                {
                    "ts": e.get("ts"),
                    "event_type": e.get("event_type"),
                    "workcell": e.get("workcell"),
                    "prospect_id": e.get("prospect_id"),
                    "opportunity_id": e.get("opportunity_id"),
                    "revenue_usd": e.get("revenue_usd"),
                    "cost_usd": e.get("cost_usd"),
                }
                for e in recent
            ],
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("command_center what_happened failed: %s", exc)
        return {"error": str(exc), "event_count": 0, "by_type": {}, "recent": []}


def _section_why(prospect_id: str | None) -> dict[str, Any]:
    """Recent decision records — the 'why' behind autonomous actions."""
    try:
        from backend.common.decision_record import list_decisions

        rows = list_decisions(prospect_id=prospect_id, limit=15)
        return {"count": len(rows), "decisions": rows}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("command_center why failed: %s", exc)
        return {"error": str(exc), "count": 0, "decisions": []}


def _section_running_now() -> dict[str, Any]:
    """Active plans + in-flight tasks + the latest control tick."""
    out: dict[str, Any] = {}
    # -- active plans + goal tree --------------------------------------------
    try:
        from backend.planning.planner import current_plans_view

        view = current_plans_view()
        out["goal_count"] = view.get("goal_count", 0)
        out["plan_count"] = view.get("plan_count", 0)
        out["active_plans"] = view.get("active_plans", [])
        out["goals"] = view.get("goals", [])
    except Exception as exc:  # noqa: BLE001
        out["plans_error"] = str(exc)
        out["active_plans"] = []
        out["goals"] = []

    # -- in-flight tasks (CRM operator-task queue proxy) ----------------------
    try:
        from backend.crm import service as crm_service

        result = crm_service.list_operator_tasks(status="open", limit=50)
        tasks = getattr(result, "tasks", None) or []
        task_list = [
            t.model_dump() if hasattr(t, "model_dump") else dict(t)
            for t in tasks
        ]
        out["open_task_count"] = int(getattr(result, "count", 0) or len(task_list))
        out["open_tasks"] = task_list[:10]
    except Exception as exc:  # noqa: BLE001
        out["tasks_error"] = str(exc)
        out["open_task_count"] = 0
        out["open_tasks"] = []

    # -- latest control tick --------------------------------------------------
    try:
        from backend.common import control_tick_ledger

        ticks = control_tick_ledger.recent_ticks(limit=1)
        tick_rows = ticks.get("ticks") if isinstance(ticks, dict) else None
        out["latest_control_tick"] = (tick_rows or [None])[-1] if tick_rows else None
    except Exception as exc:  # noqa: BLE001
        out["control_tick_error"] = str(exc)
        out["latest_control_tick"] = None
    return out


def _section_needs_approval() -> dict[str, Any]:
    """Pending operator approvals (auto-expiring stale ones on read)."""
    try:
        from backend.common.approvals import list_approvals

        rows = list_approvals(status="pending", limit=200)
        emergency = [r for r in rows if r.get("severity") == "emergency"]
        return {
            "pending_count": len(rows),
            "emergency_count": len(emergency),
            "approvals": rows[:25],
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("command_center needs_approval failed: %s", exc)
        return {"error": str(exc), "pending_count": 0, "approvals": []}


def _section_economics(day: str | None) -> dict[str, Any]:
    """Latest ROI roll-up (revenue - cost per dimension)."""
    try:
        from backend.finance.roi import get_rollup

        rollup = get_rollup(day)
        return {"rollup": rollup}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("command_center economics failed: %s", exc)
        return {"error": str(exc), "rollup": None}


def _section_health() -> dict[str, Any]:
    """4-state health aggregate + the latest entropy snapshot."""
    out: dict[str, Any] = {}
    # -- 4-state health -------------------------------------------------------
    try:
        from backend.common.health import default_registry

        out["health"] = default_registry().probe()
    except Exception as exc:  # noqa: BLE001
        out["health_error"] = str(exc)
        out["health"] = {"state": "degraded", "probes": {}}

    # -- entropy (from the latest control-tick snapshot; that pass already ran
    #    entropy.scan, so we surface it rather than re-scanning) --------------
    try:
        from backend.common import control_tick_ledger

        ticks = control_tick_ledger.recent_ticks(limit=1)
        tick_rows = ticks.get("ticks") if isinstance(ticks, dict) else None
        latest = (tick_rows or [None])[-1] if tick_rows else None
        entropy = None
        if isinstance(latest, dict):
            entropy = latest.get("entropy") or (
                (latest.get("result") or {}).get("entropy")
                if isinstance(latest.get("result"), dict) else None
            )
        out["entropy"] = entropy
    except Exception as exc:  # noqa: BLE001
        out["entropy_error"] = str(exc)
        out["entropy"] = None
    return out


def build_command_center(
    *, prospect_id: str | None = None, day: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Compose the executive aggregate. Never raises.

    ``prospect_id`` narrows the what-happened / why sections to one journey;
    ``day`` selects the economics roll-up day (default today); ``since`` bounds
    the event digest (default: last 24h).
    """
    if since is None:
        since = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return {
            "ok": True,
            "generated_at": iso_now(),
            "prospect_id": prospect_id or "",
            "what_happened": _section_what_happened(prospect_id, since),
            "why": _section_why(prospect_id),
            "running_now": _section_running_now(),
            "needs_approval": _section_needs_approval(),
            "economics": _section_economics(day),
            "health": _section_health(),
        }
    except Exception as exc:  # noqa: BLE001 — the aggregate itself never 500s
        _LOG.warning("build_command_center failed: %s", exc)
        return {"ok": False, "error": str(exc), "generated_at": iso_now()}


__all__ = ["build_command_center"]
