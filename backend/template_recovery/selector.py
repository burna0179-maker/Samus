"""Template selection + pivot-decision helpers for template-recovery.

``select_scaffold`` picks the right deterministic builder from
``SCAFFOLD_LIBRARY`` (falling back to the generic builder for unknown task
kinds). ``should_recover`` decides whether a failing step should be routed
to template_recovery at all, per the spec rule:

    if outcome == "error" and efficiency_ema < 0.45: route = "template_recovery"
"""
from __future__ import annotations

from typing import Any

from .templates import GENERIC_BUILDER, SCAFFOLD_LIBRARY, ScaffoldBuilder, version_for

# Pivot threshold: recover only when per-workcell LLM efficiency has
# degraded below this EMA. Below it, retrying the LLM burns tokens for
# diminishing returns, so a deterministic scaffold is the better route.
EFFICIENCY_PIVOT_THRESHOLD: float = 0.45


def select_scaffold(
    task_kind: str, context: dict[str, Any] | None = None
) -> tuple[ScaffoldBuilder, str, bool]:
    """Pick the deterministic template builder for ``task_kind``.

    Returns ``(builder, template_version, is_generic)``. An unknown task
    kind resolves to the generic builder with ``is_generic=True`` — this
    function never raises on an unrecognised kind. ``context`` is accepted
    for symmetry with the rest of the pipeline but is not needed to select.
    """
    builder = SCAFFOLD_LIBRARY.get(task_kind)
    if builder is not None:
        return builder, version_for(builder), False
    return GENERIC_BUILDER, version_for(GENERIC_BUILDER), True


def should_recover(
    outcome: str, efficiency_ema: float, *, threshold: float = EFFICIENCY_PIVOT_THRESHOLD
) -> bool:
    """Decide whether a failing step should pivot to template_recovery.

    Per spec: recovery triggers only when the step ended in ``error`` AND
    the per-workcell ``efficiency_ema`` has dropped below ``threshold``
    (default ``EFFICIENCY_PIVOT_THRESHOLD`` = 0.45). The boundary value
    ``efficiency_ema == threshold`` does NOT trigger — strictly below only.
    """
    return outcome == "error" and efficiency_ema < threshold


__all__ = [
    "EFFICIENCY_PIVOT_THRESHOLD",
    "select_scaffold",
    "should_recover",
]
