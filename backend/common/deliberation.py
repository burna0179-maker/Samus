"""Deliberation router — value-of-computation control over how hard to think.

The stack prices LLM *tokens* (backend/common/llm_budget.py) but never decides
how much *reasoning* a task deserves: every call is symmetric, so the system
overthinks cheap tasks and underthinks expensive ones. This module is the
missing metareasoning policy — a pure, deterministic decision that maps a task's
{value, urgency, uncertainty, reversibility} onto a reasoning depth:

    FAST < STANDARD < DEEP < DEBATE < ESCALATE

grounded in the "value of computation" idea (rational metareasoning): spend more
thought only when the answer matters, we are unsure, and a mistake is costly —
and hand off to a human when the stakes are high, irreversible, and uncertain.

It is advisory + side-effect-free: callers consult :func:`deliberate` before an
expensive reasoning step and use the returned depth to size the work (token
ceiling via :func:`depth_to_max_tokens`, whether to run a multi-model DEBATE, or
whether to ESCALATE to the ADR-0019 approval queue). Affordability is folded in
via a read-only probe of the LLM budget store, so a broke workcell is downgraded
rather than blocked. Nothing here calls an LLM.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FAST", "STANDARD", "DEEP", "DEBATE", "ESCALATE",
    "DEPTHS", "DeliberationDecision",
    "deliberate", "decide_depth", "depth_to_max_tokens", "affordable_depth",
]

# --- the reasoning-depth ladder (ordinal: cheaper -> more expensive) ---------
FAST = "fast"            # deterministic / single cheap shot
STANDARD = "standard"    # one normal LLM pass
DEEP = "deep"            # extended budget / self-consistency
DEBATE = "debate"        # multi-model corroboration (e.g. triangulation)
ESCALATE = "escalate"    # defer to a human (approval queue)

DEPTHS = (FAST, STANDARD, DEEP, DEBATE, ESCALATE)
_RANK = {d: i for i, d in enumerate(DEPTHS)}

# Token ceiling as a multiple of the workcell base budget, per depth. ESCALATE
# spends no compute — it hands the decision to a human.
_DEPTH_TOKEN_FACTOR = {FAST: 0.25, STANDARD: 1.0, DEEP: 2.0, DEBATE: 3.0, ESCALATE: 0.0}

# Score thresholds (value-of-computation). Env-overridable for tuning.
_T_FAST = 0.15
_T_STANDARD = 0.5
_T_DEEP = 1.0
# Human-escalation trigger: high value AND irreversible AND uncertain.
_ESCALATE_VALUE = 0.8
_ESCALATE_REVERSIBILITY = 0.2
_ESCALATE_UNCERTAINTY = 0.5
# Urgency caps: under time pressure the slow paths are unaffordable.
_URGENCY_CAP_HARD = 0.8   # -> at most STANDARD
_URGENCY_CAP_SOFT = 0.6   # -> at most DEEP


def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _cap(depth: str, ceiling: str) -> str:
    """Return the cheaper of ``depth`` and ``ceiling`` by ladder rank."""
    return depth if _RANK[depth] <= _RANK[ceiling] else ceiling


@dataclass
class DeliberationDecision:
    depth: str
    score: float                 # the value-of-computation score
    escalate: bool               # depth == ESCALATE (route to a human)
    debate: bool                 # depth == DEBATE (multi-model corroboration)
    max_tokens: int              # suggested token ceiling for this depth
    rationale: str
    inputs: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth, "score": round(self.score, 4),
            "escalate": self.escalate, "debate": self.debate,
            "max_tokens": self.max_tokens, "rationale": self.rationale,
            "inputs": self.inputs,
        }


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def decide_depth(
    *,
    value: float,
    urgency: float = 0.0,
    uncertainty: float = 0.5,
    reversibility: float = 1.0,
) -> tuple[str, float, str]:
    """Pure value-of-computation decision. Returns (depth, score, rationale).

    All inputs are clamped to [0,1]:
      * value         — how much the outcome is worth (importance / expected $)
      * urgency       — 1.0 = must answer now (caps the slow paths)
      * uncertainty   — 1.0 = totally unsure (raises the value of more thought)
      * reversibility — 1.0 = cheap to undo; 0.0 = irreversible (raises stakes)
    """
    value = _clamp01(value)
    urgency = _clamp01(urgency)
    uncertainty = _clamp01(uncertainty)
    reversibility = _clamp01(reversibility)
    irreversibility = 1.0 - reversibility

    # Think harder when it matters (value), we're unsure (uncertainty), and a
    # mistake is costly (irreversibility). The 0.35 floor keeps a valuable-but-
    # certain task from collapsing to FAST purely on low uncertainty.
    score = value * (0.35 + 0.65 * uncertainty) * (1.0 + irreversibility)

    # Human escalation: high-stakes + irreversible + uncertain — no amount of
    # compute should auto-commit this.
    if (
        value >= _env_float("SAMUS_DELIB_ESCALATE_VALUE", _ESCALATE_VALUE)
        and reversibility <= _env_float("SAMUS_DELIB_ESCALATE_REVERSIBILITY", _ESCALATE_REVERSIBILITY)
        and uncertainty >= _env_float("SAMUS_DELIB_ESCALATE_UNCERTAINTY", _ESCALATE_UNCERTAINTY)
    ):
        return ESCALATE, score, (
            f"escalate: high stakes (value={value:.2f}) + irreversible "
            f"(reversibility={reversibility:.2f}) + uncertain ({uncertainty:.2f})"
        )

    t_fast = _env_float("SAMUS_DELIB_T_FAST", _T_FAST)
    t_std = _env_float("SAMUS_DELIB_T_STANDARD", _T_STANDARD)
    t_deep = _env_float("SAMUS_DELIB_T_DEEP", _T_DEEP)
    if score < t_fast:
        depth = FAST
    elif score < t_std:
        depth = STANDARD
    elif score < t_deep:
        depth = DEEP
    else:
        depth = DEBATE

    rationale = f"voc score={score:.3f} -> {depth}"

    # Urgency caps: the slow paths are unaffordable under time pressure.
    if urgency >= _URGENCY_CAP_HARD:
        capped = _cap(depth, STANDARD)
        if capped != depth:
            rationale += f"; urgency={urgency:.2f} caps to {capped}"
            depth = capped
    elif urgency >= _URGENCY_CAP_SOFT:
        capped = _cap(depth, DEEP)
        if capped != depth:
            rationale += f"; urgency={urgency:.2f} caps to {capped}"
            depth = capped

    return depth, score, rationale


def depth_to_max_tokens(depth: str, base_tokens: int) -> int:
    """Suggested token ceiling for ``depth`` as a multiple of the base budget."""
    return int(max(0, base_tokens) * _DEPTH_TOKEN_FACTOR.get(depth, 1.0))


def affordable_depth(workcell: str, base_tokens: int) -> str:
    """The most expensive depth the workcell can currently afford (read-only).

    Probes the LLM budget store with :meth:`LlmBudgetStore.can_spend` (a
    pre-flight check, not a spend) from the priciest depth down. Falls back to
    DEBATE (no cap) if the budget store is unavailable — affordability must
    never harden into a block here; the real spend is still gated at call time.
    ESCALATE is never a budget outcome (it is a stakes decision, not a cost one).
    """
    try:
        from backend.common.llm_budget import get_store

        store = get_store()
    except Exception:  # noqa: BLE001 — no budget store -> don't cap
        return DEBATE
    for depth in (DEBATE, DEEP, STANDARD, FAST):
        est = depth_to_max_tokens(depth, base_tokens)
        try:
            if store.can_spend(workcell, max(1, est)).allowed:
                return depth
        except Exception:  # noqa: BLE001 — probe failure -> don't cap on this rung
            return DEBATE
    return FAST


def deliberate(
    *,
    value: float,
    urgency: float = 0.0,
    uncertainty: float = 0.5,
    reversibility: float = 1.0,
    workcell: str | None = None,
    base_tokens: int = 4000,
) -> DeliberationDecision:
    """Full decision: value-of-computation depth, capped by affordability.

    When ``workcell`` is given, the depth is additionally capped at what the
    workcell can currently afford (a broke workcell is downgraded, never an
    ESCALATE — a human hand-off is the right answer when compute is unaffordable).
    """
    depth, score, rationale = decide_depth(
        value=value, urgency=urgency, uncertainty=uncertainty,
        reversibility=reversibility,
    )
    if workcell and depth != ESCALATE:
        ceiling = affordable_depth(workcell, base_tokens)
        capped = _cap(depth, ceiling)
        if capped != depth:
            rationale += f"; budget caps {depth}->{capped}"
            depth = capped
    return DeliberationDecision(
        depth=depth,
        score=score,
        escalate=(depth == ESCALATE),
        debate=(depth == DEBATE),
        max_tokens=depth_to_max_tokens(depth, base_tokens),
        rationale=rationale,
        inputs={
            "value": _clamp01(value), "urgency": _clamp01(urgency),
            "uncertainty": _clamp01(uncertainty), "reversibility": _clamp01(reversibility),
        },
    )
