"""Orchestration for the portfolio_controller workcell.

``run_rebalance`` is the single business entry point: it builds a
:class:`~backend.portfolio_controller.signals.PortfolioSignals` vector for
each tracked workcell, runs the deterministic
:func:`~backend.portfolio_controller.allocator.rebalance` policy, persists the
decision to the shared ``samus_task_state`` table, and returns the structured
result. No LLM calls — strategy's metered proposal is never invoked.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from backend.common.task_state import write_task_state

from .allocator import WorkcellAllocation, rebalance
from .models import (
    AllocationResult,
    RebalanceRequest,
    RebalanceResponse,
    SignalsModel,
    WorkcellInput,
)
from .signals import build_signals

_LOG = logging.getLogger("samus.portfolio_controller.service")

_SERVICE = "portfolio_controller"

# Default tracked set when a caller asks for a rebalance without naming
# workcells — the LLM-spending workcells strategy itself watches plus the
# fulfillment hot path. Operators override by passing an explicit list.
DEFAULT_TRACKED_WORKCELLS: tuple[str, ...] = (
    "prospecting",
    "seo",
    "outreach",
    "fulfillment",
    "strategy",
)


def _build_allocation(item: WorkcellInput, *, store: Any = None) -> WorkcellAllocation:
    """Assemble one :class:`WorkcellAllocation` from a request item."""
    signals = build_signals(
        item.workcell,
        store=store,
        inputs={
            "queue_depth_ratio": item.queue_depth_ratio,
            "avg_task_latency_ms": item.avg_task_latency_ms,
            "token_cost_per_success": item.token_cost_per_success,
            "error_velocity": item.error_velocity,
            "throughput_efficiency": item.throughput_efficiency,
        },
    )
    return WorkcellAllocation(
        workcell=item.workcell,
        token_quota=float(item.base_token_quota),
        priority_weight=float(item.base_priority_weight),
        signals=signals,
    )


def _to_result(alloc: WorkcellAllocation) -> AllocationResult:
    """Project a post-rebalance allocation into its response model."""
    sig = alloc.signals
    return AllocationResult(
        workcell=alloc.workcell,
        token_quota=alloc.token_quota,
        priority_weight=alloc.priority_weight,
        quota_cut=alloc.quota_cut,
        priority_boosted=alloc.priority_boosted,
        signals=SignalsModel(
            queue_depth_ratio=sig.queue_depth_ratio,
            avg_task_latency_ms=sig.avg_task_latency_ms,
            token_cost_per_success=sig.token_cost_per_success,
            error_velocity=sig.error_velocity,
            throughput_efficiency=sig.throughput_efficiency,
        ),
    )


def run_rebalance(req: RebalanceRequest, *, store: Any = None) -> RebalanceResponse:
    """Run one portfolio rebalance cycle and persist the decision.

    ``store`` (an :class:`~backend.common.llm_budget.LlmBudgetStore`-shaped
    object) is injectable so tests stay deterministic and offline; in
    production it is left ``None`` and resolved lazily inside
    ``signals.build_signals``.

    When the request carries no workcells, :data:`DEFAULT_TRACKED_WORKCELLS`
    is rebalanced at default baselines. Persistence is fail-soft — a
    ``samus_task_state`` write failure is reflected in ``persisted=False``
    but never raises.
    """
    items = req.workcells or [WorkcellInput(workcell=wc) for wc in DEFAULT_TRACKED_WORKCELLS]
    allocations = [_build_allocation(it, store=store) for it in items]

    rebalance(allocations)

    results = [_to_result(a) for a in allocations]
    quota_cuts = sum(1 for a in allocations if a.quota_cut)
    priority_boosts = sum(1 for a in allocations if a.priority_boosted)
    task_id = req.task_id or f"pc-{uuid.uuid4().hex[:12]}"

    persisted = write_task_state(
        task_id=task_id,
        service=_SERVICE,
        status="completed",
        capability="plan_execution",
        decision_path="rebalance",
        workcell_count=len(results),
        quota_cuts=quota_cuts,
        priority_boosts=priority_boosts,
        allocations=[r.model_dump() for r in results],
    )

    _LOG.info(
        "rebalance: task=%s workcells=%d quota_cuts=%d priority_boosts=%d persisted=%s",
        task_id,
        len(results),
        quota_cuts,
        priority_boosts,
        persisted,
    )

    return RebalanceResponse(
        task_id=task_id,
        decision_path="rebalance",
        capability="plan_execution",
        workcell_count=len(results),
        quota_cuts=quota_cuts,
        priority_boosts=priority_boosts,
        allocations=results,
        persisted=persisted,
    )


__all__ = ["run_rebalance", "DEFAULT_TRACKED_WORKCELLS"]
