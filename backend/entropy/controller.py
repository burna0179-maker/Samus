"""Countermeasure decision logic for the entropy workcell.

Maps entropy conditions to deterministic, autonomous countermeasures. Pure
logic — no LLM, no I/O. Each threshold is a named constant with a one-line
rationale; the decision function returns an *ordered* list of countermeasures
(most-systemic first) so the orchestrator can apply them in priority order.
"""
from __future__ import annotations

from .metrics import EntropyInputs

# ── Countermeasure identifiers (the spec's response vocabulary) ──────────────
FREEZE_NONESSENTIAL_LLM_PATHS: str = "freeze_nonessential_llm_paths"
ACTIVATE_DETERMINISTIC_MODE: str = "activate_deterministic_mode"
REDUCE_QUOTA: str = "reduce_quota"
CLONE_TEMPLATE_STRATEGY: str = "clone_template_strategy"
TIGHTEN_PROSPECT_FILTERING: str = "tighten_prospect_filtering"

# ── Threshold constants ──────────────────────────────────────────────────────
# Retry rate above this signals "rising retries" — retries compound load and
# precede cascade failure, so freeze non-essential LLM paths early.
RETRY_RATE_THRESHOLD: float = 0.30
# Queue variance above this signals saturation/thrash — switch to the
# deterministic (no-LLM) path so throughput stops depending on a flaky model.
QUEUE_SATURATION_THRESHOLD: float = 0.60
# Efficiency EMA below this means LLM spend is producing little value — cut
# quota so a low-efficiency workcell stops burning budget under stress.
EFFICIENCY_EMA_THRESHOLD: float = 0.40
# A template route that succeeded recently is a known-good fallback — when the
# system is unstable, clone it rather than re-deriving a strategy.
TEMPLATE_SUCCESS_THRESHOLD: float = 0.50
# Token/success ratio above this means each conversion costs too many tokens —
# tighten prospect filtering so only high-probability leads consume the model.
TOKEN_SUCCESS_RATIO_THRESHOLD: float = 2.0


def recommend_countermeasures(
    inputs: EntropyInputs,
    *,
    efficiency_ema: float | None = None,
    token_success_ratio: float | None = None,
    template_success_rate: float | None = None,
) -> list[str]:
    """Return the ordered list of recommended countermeasures.

    ``inputs`` carries the core instability signals. The extra signals are
    optional — when ``None`` their corresponding rule is simply not evaluated:

    - ``efficiency_ema``        — LLM-call success EMA in ``[0.0, 1.0]``.
    - ``token_success_ratio``   — tokens spent per successful conversion.
    - ``template_success_rate`` — recent success rate of a template route.

    Ordering: most-systemic first (deterministic-mode flip / LLM freeze) so the
    orchestrator applies the broadest stabiliser before the narrower ones.
    """
    measures: list[str] = []

    # Queue saturation → strip the LLM dependency out of the hot path entirely.
    if inputs.queue_variance >= QUEUE_SATURATION_THRESHOLD:
        measures.append(ACTIVATE_DETERMINISTIC_MODE)

    # Rising retries → halt non-essential LLM paths before retries cascade.
    if inputs.task_retry_rate >= RETRY_RATE_THRESHOLD:
        measures.append(FREEZE_NONESSENTIAL_LLM_PATHS)

    # Low efficiency EMA → reduce the LLM quota for the struggling workcell.
    if efficiency_ema is not None and efficiency_ema < EFFICIENCY_EMA_THRESHOLD:
        measures.append(REDUCE_QUOTA)

    # Rising token/success ratio → tighten prospect filtering to cut waste.
    if (
        token_success_ratio is not None
        and token_success_ratio > TOKEN_SUCCESS_RATIO_THRESHOLD
    ):
        measures.append(TIGHTEN_PROSPECT_FILTERING)

    # A recently-successful template route → clone it as the safe fallback.
    if (
        template_success_rate is not None
        and template_success_rate >= TEMPLATE_SUCCESS_THRESHOLD
    ):
        measures.append(CLONE_TEMPLATE_STRATEGY)

    return measures


__all__ = [
    "recommend_countermeasures",
    "FREEZE_NONESSENTIAL_LLM_PATHS",
    "ACTIVATE_DETERMINISTIC_MODE",
    "REDUCE_QUOTA",
    "CLONE_TEMPLATE_STRATEGY",
    "TIGHTEN_PROSPECT_FILTERING",
    "RETRY_RATE_THRESHOLD",
    "QUEUE_SATURATION_THRESHOLD",
    "EFFICIENCY_EMA_THRESHOLD",
    "TEMPLATE_SUCCESS_THRESHOLD",
    "TOKEN_SUCCESS_RATIO_THRESHOLD",
]
