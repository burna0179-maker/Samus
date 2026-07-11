"""Per-workcell portfolio signal vector + a deterministic builder.

``PortfolioSignals`` is the float-only feature vector the allocator routes
on. The builder assembles it for one workcell from whatever inputs are
available — primarily ``efficiency_ema`` read off the shared LLM budget
store (contract §4) — and mirrors strategy's
``efficiency_ema_by_workcell: dict[str, float]`` shape so the two subsystems
stay interoperable.

Everything here is pure / deterministic: no network, no LLM, no clock reads.
The only I/O is the budget-store snapshot read, which is itself fail-soft
(JSON fallback) and trivially monkeypatchable in tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "PortfolioSignals",
    "efficiency_ema_for",
    "efficiency_ema_by_workcell",
    "build_signals",
]


@dataclass
class PortfolioSignals:
    """Float-only signal vector for one workcell.

    ``queue_depth_ratio``       — pending / capacity; >1.0 means backlog.
    ``avg_task_latency_ms``     — mean wall-clock per task, milliseconds.
    ``token_cost_per_success``  — LLM tokens burned per successful task.
    ``error_velocity``          — fraction of recent calls ending in error
                                  (0.0-1.0); the quota-cut driver.
    ``throughput_efficiency``   — success-weighted throughput score
                                  (0.0-1.0); the priority-boost driver.
    """

    queue_depth_ratio: float = 0.0
    avg_task_latency_ms: float = 0.0
    token_cost_per_success: float = 0.0
    error_velocity: float = 0.0
    throughput_efficiency: float = 0.0


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float, falling back to ``default`` on bad input."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def efficiency_ema_for(workcell: str, *, store: Any = None) -> float:
    """Read ``efficiency_ema`` for one workcell off the LLM budget store.

    Fail-soft: any store error (missing key, store outage) collapses to
    ``1.0`` — the same benefit-of-the-doubt default the budget store itself
    initialises ``efficiency_ema`` with — so a controller never punishes a
    workcell for a transient persistence fault.
    """
    try:
        if store is None:
            from backend.common.llm_budget import get_store

            store = get_store()
        snap = store.snapshot(workcell)
        return _f(getattr(snap, "efficiency_ema", 1.0), 1.0)
    except Exception:  # noqa: BLE001 — observability read, never fatal
        return 1.0


def efficiency_ema_by_workcell(
    workcells: tuple[str, ...] | list[str], *, store: Any = None
) -> dict[str, float]:
    """Build strategy's ``efficiency_ema_by_workcell`` shape for the given set.

    Mirrors :attr:`backend.strategy.triggers.TriggerContext.efficiency_ema_by_workcell`
    so a portfolio decision can be handed straight into the strategy trigger
    layer for context without reshaping.
    """
    if store is None:
        try:
            from backend.common.llm_budget import get_store

            store = get_store()
        except Exception:  # noqa: BLE001
            store = None
    return {wc: efficiency_ema_for(wc, store=store) for wc in workcells}


def build_signals(
    workcell: str,
    *,
    store: Any = None,
    inputs: dict[str, Any] | None = None,
) -> PortfolioSignals:
    """Deterministically assemble :class:`PortfolioSignals` for ``workcell``.

    ``efficiency_ema`` (range 0.0-1.0) is the anchor signal read off the
    budget store. The two routing fields derive from it directly:

    - ``error_velocity``        = ``1.0 - efficiency_ema`` — a workcell whose
      LLM EMA has decayed toward 0.0 is, by the budget store's own
      definition, accumulating failures fast.
    - ``throughput_efficiency`` = ``efficiency_ema`` — a high EMA is
      success-weighted throughput.

    ``inputs`` may supply observed operational metrics
    (``queue_depth_ratio``, ``avg_task_latency_ms``,
    ``token_cost_per_success``) and may also explicitly override
    ``error_velocity`` / ``throughput_efficiency``. Anything absent falls
    back to the EMA-derived value (for the routing fields) or 0.0. The
    function is pure given a fixed store snapshot — same inputs, same output.
    """
    ema = efficiency_ema_for(workcell, store=store)
    src = inputs or {}
    return PortfolioSignals(
        queue_depth_ratio=_f(src.get("queue_depth_ratio"), 0.0),
        avg_task_latency_ms=_f(src.get("avg_task_latency_ms"), 0.0),
        token_cost_per_success=_f(src.get("token_cost_per_success"), 0.0),
        error_velocity=_f(src.get("error_velocity"), 1.0 - ema),
        throughput_efficiency=_f(src.get("throughput_efficiency"), ema),
    )
