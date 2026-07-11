"""Core deterministic routing logic for the path-optimizer workcell.

No LLM calls, no I/O — pure functions over a small ``WorkcellState`` value.
The thresholds are module-level named constants so the routing policy is
auditable and tunable in one place.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# -- routing thresholds (the whole policy lives here) -----------------------

#: At/above this efficiency EMA the workcell is trusted to run autonomously.
AUTONOMOUS_EMA_THRESHOLD: float = 0.82
#: At/above this EMA (but below autonomous) the workcell runs hybrid-template.
HYBRID_EMA_THRESHOLD: float = 0.55
#: Fraction of recent outcomes that must be errors to declare an error spike.
ERROR_SPIKE_RATIO: float = 0.35
#: How many recent outcomes the spike detector inspects.
ERROR_SPIKE_WINDOW: int = 20

# -- the four execution routes ----------------------------------------------

PATH_AUTONOMOUS_LLM = "autonomous_llm"
PATH_HYBRID_TEMPLATE = "hybrid_template"
PATH_DETERMINISTIC_SCAFFOLD = "deterministic_scaffold"
PATH_SAFE_STATIC_FALLBACK = "safe_static_fallback"


@dataclass(frozen=True)
class WorkcellState:
    """Minimal performance snapshot the optimizer routes on."""

    workcell: str
    efficiency_ema: float
    error_spike_detected: bool


def detect_error_spike(history: Iterable[Any]) -> bool:
    """Return ``True`` when recent outcomes show an error spike.

    Inspects the last ``ERROR_SPIKE_WINDOW`` entries and returns ``True`` when
    the fraction with ``outcome == "error"`` is at or above
    ``ERROR_SPIKE_RATIO``. Each entry may be a mapping (``{"outcome": ...}``)
    or an object exposing an ``outcome`` attribute. An empty history is never
    a spike.
    """
    entries = list(history)
    last_window = entries[-ERROR_SPIKE_WINDOW:]
    if not last_window:
        return False

    errors = sum(1 for entry in last_window if _outcome_of(entry) == "error")
    return errors / max(len(last_window), 1) >= ERROR_SPIKE_RATIO


def _outcome_of(entry: Any) -> str:
    """Extract the ``outcome`` field from a mapping or attribute-bearing object."""
    if isinstance(entry, dict):
        return str(entry.get("outcome", ""))
    return str(getattr(entry, "outcome", ""))


class PathOptimizer:
    """Selects an execution route from a :class:`WorkcellState`."""

    def select_execution_path(self, workcell_state: WorkcellState) -> str:
        """Return the execution route for ``workcell_state``.

        Policy (evaluated top-down):

        * ``efficiency_ema >= 0.82`` → ``autonomous_llm``
        * ``efficiency_ema >= 0.55`` → ``hybrid_template``
        * ``error_spike_detected``   → ``deterministic_scaffold``
        * otherwise                  → ``safe_static_fallback``
        """
        ema = workcell_state.efficiency_ema
        if ema >= AUTONOMOUS_EMA_THRESHOLD:
            return PATH_AUTONOMOUS_LLM
        if ema >= HYBRID_EMA_THRESHOLD:
            return PATH_HYBRID_TEMPLATE
        if workcell_state.error_spike_detected:
            return PATH_DETERMINISTIC_SCAFFOLD
        return PATH_SAFE_STATIC_FALLBACK


__all__ = [
    "AUTONOMOUS_EMA_THRESHOLD",
    "HYBRID_EMA_THRESHOLD",
    "ERROR_SPIKE_RATIO",
    "ERROR_SPIKE_WINDOW",
    "PATH_AUTONOMOUS_LLM",
    "PATH_HYBRID_TEMPLATE",
    "PATH_DETERMINISTIC_SCAFFOLD",
    "PATH_SAFE_STATIC_FALLBACK",
    "WorkcellState",
    "PathOptimizer",
    "detect_error_spike",
]
