"""Planning data shapes — Goal, Assumption, PlanStep, Plan.

Pure dataclasses, no I/O. The persistence layer (``store.py``) serialises
these to/from the DDB + JSON stores; the planner (``planner.py``) builds them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --- Goal horizons ---------------------------------------------------------
HORIZON_YEAR = "year"
HORIZON_90D = "90d"
HORIZON_30D = "30d"
HORIZON_WEEK = "week"
HORIZON_DAY = "day"

HORIZONS: tuple[str, ...] = (
    HORIZON_YEAR, HORIZON_90D, HORIZON_30D, HORIZON_WEEK, HORIZON_DAY,
)

# --- statuses --------------------------------------------------------------
GOAL_ACTIVE = "active"
GOAL_MET = "met"
GOAL_MISSED = "missed"
GOAL_ARCHIVED = "archived"

PLAN_ACTIVE = "active"
PLAN_SUPERSEDED = "superseded"
PLAN_ARCHIVED = "archived"


@dataclass
class Goal:
    """One node in the multi-horizon goal tree.

    ``target_metric`` names what is measured (e.g. ``revenue_usd``,
    ``leads_created``, ``deals_closed``). ``parent_id`` links a child horizon
    to its parent (daily -> weekly -> 30d -> 90d -> year); the root year goal
    has an empty ``parent_id``.
    """

    id: str
    horizon: str
    target_metric: str
    target_value: float
    parent_id: str = ""
    status: str = GOAL_ACTIVE
    label: str = ""
    period_start: str = ""      # ISO date the horizon window opens (optional)
    period_end: str = ""        # ISO date the horizon window closes (optional)
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Goal":
        return cls(
            id=str(row.get("id") or ""),
            horizon=str(row.get("horizon") or ""),
            target_metric=str(row.get("target_metric") or ""),
            target_value=float(row.get("target_value") or 0.0),
            parent_id=str(row.get("parent_id") or ""),
            status=str(row.get("status") or GOAL_ACTIVE),
            label=str(row.get("label") or ""),
            period_start=str(row.get("period_start") or ""),
            period_end=str(row.get("period_end") or ""),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            metadata=dict(row.get("metadata") or {}),
        )


@dataclass
class Assumption:
    """A checkable predicate a plan depends on.

    Evaluated against the unified event stream. ``metric`` is a known key the
    evaluator can compute (see ``planner._ASSUMPTION_METRICS``); ``op`` is a
    comparison (``>=``, ``>``, ``<=``, ``<``); ``threshold`` the bound;
    ``window_days`` the look-back window (0 => same-day / lifetime as the
    metric defines).
    """

    id: str
    description: str
    metric: str
    op: str
    threshold: float
    window_days: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Assumption":
        return cls(
            id=str(row.get("id") or ""),
            description=str(row.get("description") or ""),
            metric=str(row.get("metric") or ""),
            op=str(row.get("op") or ">="),
            threshold=float(row.get("threshold") or 0.0),
            window_days=int(row.get("window_days") or 1),
        )


@dataclass
class PlanStep:
    """One ordered action in a plan (advisory — the arbiter/dispatcher
    executes the concrete work; the plan records intent + rationale)."""

    name: str
    channel: str = ""          # call | email | seo | retention | ""
    action: str = ""
    target_value: float = 0.0  # e.g. sends/day, calls/day this step commits to
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "PlanStep":
        return cls(
            name=str(row.get("name") or ""),
            channel=str(row.get("channel") or ""),
            action=str(row.get("action") or ""),
            target_value=float(row.get("target_value") or 0.0),
            rationale=str(row.get("rationale") or ""),
            metadata=dict(row.get("metadata") or {}),
        )


@dataclass
class Plan:
    """A persisted plan toward one goal — the replacement for the one-shot
    MAPE-K ``run_cycle`` output.

    ``plan_generation`` increments on every replan of the same goal (gen 1 is
    the initial plan; a violated assumption produces gen 2 = "Plan B", etc.).
    Only the latest generation per goal is ``active``; superseded generations
    are retained (audit) with ``status = superseded``.
    """

    id: str
    goal_id: str
    plan_generation: int = 1
    status: str = PLAN_ACTIVE
    strategy: str = "revenue-decomposition"
    assumptions: list[Assumption] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    rationale: str = ""
    created_at: str = ""
    updated_at: str = ""
    decision_id: str = ""       # the DecisionRecord minted at generation time
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Plan":
        return cls(
            id=str(row.get("id") or ""),
            goal_id=str(row.get("goal_id") or ""),
            plan_generation=int(row.get("plan_generation") or 1),
            status=str(row.get("status") or PLAN_ACTIVE),
            strategy=str(row.get("strategy") or "revenue-decomposition"),
            assumptions=[
                Assumption.from_dict(a) for a in (row.get("assumptions") or [])
                if isinstance(a, dict)
            ],
            steps=[
                PlanStep.from_dict(s) for s in (row.get("steps") or [])
                if isinstance(s, dict)
            ],
            rationale=str(row.get("rationale") or ""),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            decision_id=str(row.get("decision_id") or ""),
            metadata=dict(row.get("metadata") or {}),
        )


__all__ = [
    "HORIZON_YEAR", "HORIZON_90D", "HORIZON_30D", "HORIZON_WEEK", "HORIZON_DAY",
    "HORIZONS",
    "GOAL_ACTIVE", "GOAL_MET", "GOAL_MISSED", "GOAL_ARCHIVED",
    "PLAN_ACTIVE", "PLAN_SUPERSEDED", "PLAN_ARCHIVED",
    "Goal", "Assumption", "PlanStep", "Plan",
]
