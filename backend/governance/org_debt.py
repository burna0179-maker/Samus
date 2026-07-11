"""Organizational debt — which workcell is a struggling "employee"?

Topic-3 (Organizational Theory) delta. Samus already tracks per-workcell trust
(governance/karma), performance (llm_budget efficiency EMA), and failure
(circuit breaker), but nothing aggregates them into a single "is this unit in
trouble?" score. This module is that aggregate: treating each workcell as an
employee, ``org_debt`` blends low trust + low efficiency + recent circuit trips
into a 0..1 debt score (1 = maximum trouble), so the operator (or the
portfolio_controller acting as department manager) can see at a glance which
units are accumulating organizational debt.

Read-only over the existing stores; both are injectable for testing. Fail-soft —
a missing store degrades to the neutral baseline rather than raising.

NOTE (single-agent reality): Samus is one agent of ~25 deterministically-
orchestrated workcells, not a fleet with competing incentives, so this is an
internal management signal, not an inter-agent trust market.
"""

from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger("samus.governance.org_debt")

# Blend weights: trust, efficiency, and acute failure. Sum to 1.0.
_W_KARMA = 0.4
_W_EFFICIENCY = 0.4
_W_CIRCUIT = 0.2
_CIRCUIT_ERROR_SCALE = 10.0  # consecutive_errors that maps to full circuit penalty

__all__ = ["workcell_debt", "org_debt_report", "DEFAULT_WORKCELLS"]

# The workcells the portfolio controller tracks by default — kept local so this
# module does not hard-depend on the controller's import graph.
DEFAULT_WORKCELLS = (
    "prospecting",
    "outreach",
    "seo",
    "proposal",
    "crm",
    "finance",
    "voice",
)


def _karma_health(workcell: str, karma_store: Any | None) -> float:
    """Mean of the karma dimensions [0,1]; neutral 0.5 when unavailable."""
    try:
        store = karma_store
        if store is None:
            from backend.governance.karma.store import get_store

            store = get_store()
        vec = store.load(workcell)
        dims = vec.dims()
        return round(sum(dims.values()) / len(dims), 4) if dims else 0.5
    except Exception as exc:  # noqa: BLE001 — missing karma -> neutral
        _LOG.debug("karma read failed for %s: %s", workcell, exc)
        return 0.5


def _budget_signals(workcell: str, budget_store: Any | None) -> tuple[float, float]:
    """Return (efficiency_ema, circuit_penalty) for a workcell; neutral on error."""
    try:
        store = budget_store
        if store is None:
            from backend.common.llm_budget import get_store

            store = get_store()
        b = store.snapshot(workcell)
        eff = float(getattr(b, "efficiency_ema", 1.0))
        if getattr(b, "circuit_open_until", ""):
            circuit_penalty = 1.0
        else:
            errors = int(getattr(b, "consecutive_errors", 0) or 0)
            circuit_penalty = min(1.0, errors / _CIRCUIT_ERROR_SCALE)
        return eff, circuit_penalty
    except Exception as exc:  # noqa: BLE001 — missing budget -> neutral
        _LOG.debug("budget read failed for %s: %s", workcell, exc)
        return 1.0, 0.0


def workcell_debt(
    workcell: str,
    *,
    karma_store: Any | None = None,
    budget_store: Any | None = None,
) -> dict[str, Any]:
    """Aggregate org-debt score for one workcell (1.0 = maximum trouble)."""
    karma_health = _karma_health(workcell, karma_store)
    efficiency, circuit_penalty = _budget_signals(workcell, budget_store)
    debt = (
        _W_KARMA * (1.0 - karma_health)
        + _W_EFFICIENCY * (1.0 - efficiency)
        + _W_CIRCUIT * circuit_penalty
    )
    return {
        "workcell": workcell,
        "org_debt": round(debt, 4),
        "karma_health": round(karma_health, 4),
        "efficiency": round(efficiency, 4),
        "circuit_penalty": round(circuit_penalty, 4),
    }


def org_debt_report(
    workcells: list[str] | None = None,
    *,
    karma_store: Any | None = None,
    budget_store: Any | None = None,
) -> dict[str, Any]:
    """Debt score for each workcell, ranked most-indebted first."""
    cells = list(workcells) if workcells is not None else list(DEFAULT_WORKCELLS)
    rows = [workcell_debt(w, karma_store=karma_store, budget_store=budget_store) for w in cells]
    rows.sort(key=lambda r: r["org_debt"], reverse=True)
    total = round(sum(r["org_debt"] for r in rows), 4)
    return {
        "workcells": rows,
        "total_org_debt": total,
        "worst": rows[0]["workcell"] if rows else None,
    }
