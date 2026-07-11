"""Persistence for goals + plans — DDB-first, JSON-file fallback.

Follows the ``backend/common/approvals.py`` table pattern verbatim: a DDB table
(``samus_goals`` / ``samus_plans``, PK=``id``) is attempted first when
configured, with a JSON-file fallback under the writable state root so a dev
box / test run needs no AWS. Writes never raise; the JSON mirror is always
attempted so local reads work even when DDB is unreachable.

Goals and plans are MUTABLE entities (a goal's status flips active->met; the
latest plan generation is active, older ones superseded), so — unlike the
append-only business-event ledger — each id is a row that is overwritten in
place. That is exactly the approval-store shape.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from backend.common.dates import iso_now

from .models import Goal, Plan

_LOG = logging.getLogger("samus.planning.store")

_JSON_LOCK = threading.Lock()

_GOALS_ENV = "SAMUS_GOALS_PATH"
_PLANS_ENV = "SAMUS_PLANS_PATH"


# ---------------------------------------------------------------------------
# Path resolution (env override -> state root)
# ---------------------------------------------------------------------------


def _json_path(env_var: str, *parts: str) -> str:
    override = os.getenv(env_var)
    if override:
        return override
    from backend.common.state_paths import state_path

    return str(state_path(*parts))


def _goals_path() -> str:
    return _json_path(_GOALS_ENV, "planning", "goals.json")


def _plans_path() -> str:
    return _json_path(_PLANS_ENV, "planning", "plans.json")


# ---------------------------------------------------------------------------
# JSON fallback (dict keyed by id)
# ---------------------------------------------------------------------------


def _json_load_all(path: str) -> dict[str, dict[str, Any]]:
    with _JSON_LOCK:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except (OSError, ValueError) as exc:
            _LOG.warning("planning json load failed (%s): %s", path, exc)
            return {}


def _json_save(path: str, row: dict[str, Any]) -> bool:
    with _JSON_LOCK:
        data: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except (OSError, ValueError):
                data = {}
        data[row["id"]] = row
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
            os.replace(tmp, path)
            return True
        except (OSError, ValueError) as exc:
            _LOG.warning("planning json save failed (%s): %s", path, exc)
            return False


# ---------------------------------------------------------------------------
# DDB (best-effort; JSON is the safety net)
# ---------------------------------------------------------------------------


def _ddb_table(table_attr: str) -> Any | None:
    try:
        from backend.common import aws
        from backend.common.config import get_settings

        s = get_settings()
        table_name = getattr(s, table_attr, "") or ""
        if not table_name:
            return None
        return aws.table(table_name, s.aws_region)
    except Exception as exc:  # noqa: BLE001 — fallback path
        _LOG.debug("planning ddb table unavailable (%s): %s", table_attr, exc)
        return None


def _ddb_put(table_attr: str, row: dict[str, Any]) -> bool:
    table = _ddb_table(table_attr)
    if table is None:
        return False
    try:
        # DDB rejects raw floats — serialise nested structures as a JSON blob
        # and keep only the id + a few flat scalars queryable. Mirrors how the
        # approvals/roi stores keep payloads JSON-encoded.
        item = {"id": row["id"], "payload": json.dumps(row, default=str)}
        table.put_item(Item=item)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("planning ddb put failed (%s): %s", table_attr, exc)
        return False


def _ddb_scan(table_attr: str) -> list[dict[str, Any]] | None:
    table = _ddb_table(table_attr)
    if table is None:
        return None
    try:
        resp = table.scan()
        items = list(resp.get("Items") or [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items") or [])
        out: list[dict[str, Any]] = []
        for item in items:
            payload = item.get("payload")
            if isinstance(payload, str):
                try:
                    out.append(json.loads(payload))
                    continue
                except ValueError:
                    pass
            out.append(_plain(item))
        return out
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("planning ddb scan failed (%s): %s", table_attr, exc)
        return None


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    from decimal import Decimal

    def conv(v: Any) -> Any:
        if isinstance(v, Decimal):
            f = float(v)
            return int(f) if f.is_integer() else f
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    return {k: conv(v) for k, v in item.items()}


def _load_all(table_attr: str, path: str) -> dict[str, dict[str, Any]]:
    merged = _json_load_all(path)
    ddb_rows = _ddb_scan(table_attr)
    if ddb_rows:
        for item in ddb_rows:
            rid = str(item.get("id") or "")
            if rid:
                merged[rid] = {**merged.get(rid, {}), **item}
    return merged


def _save(table_attr: str, path: str, row: dict[str, Any]) -> None:
    wrote_ddb = _ddb_put(table_attr, row)
    wrote_json = _json_save(path, row)
    if not wrote_ddb and not wrote_json:
        _LOG.warning("planning row %s not persisted anywhere", row.get("id"))


# ---------------------------------------------------------------------------
# Goals API
# ---------------------------------------------------------------------------


def save_goal(goal: Goal) -> Goal:
    """Persist one goal (overwrite by id). Stamps updated_at. Never raises."""
    try:
        if not goal.created_at:
            goal.created_at = iso_now()
        goal.updated_at = iso_now()
        _save("ddb_goals_table", _goals_path(), goal.to_dict())
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("save_goal failed: %s", exc)
    return goal


def save_goals(goals: list[Goal]) -> list[Goal]:
    return [save_goal(g) for g in goals]


def get_goal(goal_id: str) -> Goal | None:
    try:
        row = _load_all("ddb_goals_table", _goals_path()).get(str(goal_id))
        return Goal.from_dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("get_goal failed: %s", exc)
        return None


def list_goals(
    *,
    horizon: str | None = None,
    status: str | None = None,
    parent_id: str | None = None,
) -> list[Goal]:
    try:
        rows = _load_all("ddb_goals_table", _goals_path()).values()
        goals = [Goal.from_dict(r) for r in rows if isinstance(r, dict)]
        if horizon:
            goals = [g for g in goals if g.horizon == horizon]
        if status:
            goals = [g for g in goals if g.status == status]
        if parent_id is not None:
            goals = [g for g in goals if g.parent_id == parent_id]
        goals.sort(key=lambda g: (g.horizon, g.created_at))
        return goals
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("list_goals failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Plans API
# ---------------------------------------------------------------------------


def save_plan(plan: Plan) -> Plan:
    try:
        if not plan.created_at:
            plan.created_at = iso_now()
        plan.updated_at = iso_now()
        _save("ddb_plans_table", _plans_path(), plan.to_dict())
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("save_plan failed: %s", exc)
    return plan


def get_plan(plan_id: str) -> Plan | None:
    try:
        row = _load_all("ddb_plans_table", _plans_path()).get(str(plan_id))
        return Plan.from_dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("get_plan failed: %s", exc)
        return None


def list_plans(
    *,
    goal_id: str | None = None,
    status: str | None = None,
) -> list[Plan]:
    try:
        rows = _load_all("ddb_plans_table", _plans_path()).values()
        plans = [Plan.from_dict(r) for r in rows if isinstance(r, dict)]
        if goal_id:
            plans = [p for p in plans if p.goal_id == goal_id]
        if status:
            plans = [p for p in plans if p.status == status]
        plans.sort(key=lambda p: (p.goal_id, p.plan_generation))
        return plans
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("list_plans failed: %s", exc)
        return []


def active_plan_for_goal(goal_id: str) -> Plan | None:
    """The single active (latest-generation) plan for a goal, if any."""
    actives = [p for p in list_plans(goal_id=goal_id) if p.status == "active"]
    if not actives:
        return None
    actives.sort(key=lambda p: p.plan_generation)
    return actives[-1]


def latest_generation_for_goal(goal_id: str) -> int:
    plans = list_plans(goal_id=goal_id)
    return max((p.plan_generation for p in plans), default=0)


__all__ = [
    "save_goal",
    "save_goals",
    "get_goal",
    "list_goals",
    "save_plan",
    "get_plan",
    "list_plans",
    "active_plan_for_goal",
    "latest_generation_for_goal",
]
