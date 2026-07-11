"""Orchestrator for the path-optimizer workcell.

Resolves the live ``efficiency_ema`` for the target workcell from the LLM
budget store, computes ``error_spike_detected`` from the supplied outcome
history, runs the deterministic :class:`PathOptimizer`, persists the decision
to ``samus_task_state``, and returns the structured result.
"""
from __future__ import annotations

import logging

from backend.common.llm_budget import get_store
from backend.common.task_state import write_task_state

from .models import SelectPathRequest, SelectPathResponse
from .router import PathOptimizer, WorkcellState, detect_error_spike

_LOG = logging.getLogger("samus.path_optimizer.service")

_SERVICE = "path_optimizer"
_CAPABILITY = "plan_execution"


def _resolve_efficiency_ema(workcell: str) -> float:
    """Read the live ``efficiency_ema`` for ``workcell`` from the budget store.

    Fail-soft: an unknown or never-metered workcell yields a fresh budget row
    whose EMA defaults to ``1.0`` (benefit of the doubt). A store outage is
    swallowed and treated the same way — routing must never block on telemetry.
    """
    try:
        return float(get_store().snapshot(workcell).efficiency_ema)
    except Exception as exc:  # noqa: BLE001 — fail-soft: telemetry is non-critical
        _LOG.warning("efficiency_ema lookup failed for %s: %s", workcell, exc)
        return 1.0


def select_path(request: SelectPathRequest, *, task_id: str = "") -> SelectPathResponse:
    """Choose the execution route for ``request.workcell`` and persist it.

    ``task_id`` is used as the partition key when writing task state; an empty
    value falls back to a synthetic ``path_optimizer:<workcell>`` key so the
    write still lands.
    """
    efficiency_ema = _resolve_efficiency_ema(request.workcell)
    error_spike_detected = detect_error_spike(request.history)

    state = WorkcellState(
        workcell=request.workcell,
        efficiency_ema=efficiency_ema,
        error_spike_detected=error_spike_detected,
    )
    decision_path = PathOptimizer().select_execution_path(state)

    _LOG.info(
        "path_optimizer: workcell=%s ema=%.4f spike=%s history=%d -> %s",
        request.workcell,
        efficiency_ema,
        error_spike_detected,
        len(request.history),
        decision_path,
    )

    write_task_state(
        task_id=task_id or f"{_SERVICE}:{request.workcell}",
        service=_SERVICE,
        capability=_CAPABILITY,
        decision_path=decision_path,
        efficiency_ema=efficiency_ema,
        error_spike_detected=error_spike_detected,
    )

    return SelectPathResponse(
        workcell=request.workcell,
        decision_path=decision_path,
        efficiency_ema=efficiency_ema,
        error_spike_detected=error_spike_detected,
        history_size=len(request.history),
    )


__all__ = ["select_path"]
