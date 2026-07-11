"""Attribution decide/learn engine — UCB1 over email-variant arms.

``build_arm_id`` makes the variant key; ``select_variant`` chooses among
candidate variants (UCB1, cold arms explored first); ``record_outcome`` learns
from a bounded [0,1] reward; ``reward_from_signals`` shapes that reward from
open/click/reply/close signals.

Wire-dormant: when ``settings.attribution_enabled`` is false, ``select_variant``
is a deterministic passthrough (first candidate = current behavior), so the
engine is fully built but changes nothing until armed. ``record_outcome`` always
accrues stats (so a track record exists before arming).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from backend.common.config import get_settings
from backend.attribution import store as attr_store
from backend.attribution.store import VariantStats

_LOG = logging.getLogger("samus.attribution.engine")

_SEP = "::"
_UCB_C = 1.4142135623730951  # sqrt(2)

# Reward weights for the bounded [0,1] reward (won alone -> 1.0).
_W_OPEN = 0.1
_W_CLICK = 0.25
_W_REPLY = 0.6
_W_WON = 1.0


def _enabled() -> bool:
    return bool(getattr(get_settings(), "attribution_enabled", False))


def _iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def build_arm_id(template_id: str, subject_variant: str = "", cta_variant: str = "") -> str:
    """Compose the variant arm key ``template::subject::cta``. Blank slots are
    normalized to ``default`` so a template with no explicit variants is one
    stable arm."""
    parts = [
        (template_id or "default").strip(),
        (subject_variant or "default").strip(),
        (cta_variant or "default").strip(),
    ]
    return _SEP.join(p or "default" for p in parts)


def reward_from_signals(
    *, opened: bool = False, clicked: bool = False,
    replied: bool = False, won: bool = False,
) -> float:
    """Shape engagement/close signals into a bounded [0,1] reward."""
    raw = (
        _W_OPEN * bool(opened) + _W_CLICK * bool(clicked)
        + _W_REPLY * bool(replied) + _W_WON * bool(won)
    )
    return max(0.0, min(1.0, raw))


@dataclass(frozen=True)
class VariantChoice:
    arm_id: str
    reason: str
    score: float = 0.0


def select_variant(candidate_arm_ids: list[str], *, now: float | None = None) -> VariantChoice:
    """Choose a variant arm among candidates via UCB1.

    Deterministic passthrough (first candidate) when disabled or given a single
    candidate. Cold arms (0 trials) are explored first; otherwise the arm with
    the highest UCB1 score wins. Fail-open: any error -> first candidate.
    """
    cands = [c for c in (candidate_arm_ids or []) if c]
    if not cands:
        return VariantChoice(arm_id="", reason="no_candidates")
    if not _enabled() or len(cands) == 1:
        return VariantChoice(arm_id=cands[0], reason="passthrough" if len(cands) > 1 else "single")
    try:
        store = attr_store.get_store()
        stats = {a: (store.load(a) or VariantStats(arm_id=a)) for a in cands}
        total = sum(s.trials for s in stats.values())
        # Explore any never-tried arm first.
        for a in cands:
            if stats[a].trials == 0:
                return VariantChoice(arm_id=a, reason="explore_cold", score=float("inf"))
        ln_total = math.log(max(1, total))
        best_arm, best_score = cands[0], float("-inf")
        for a in cands:
            s = stats[a]
            ucb = s.mean_reward + _UCB_C * math.sqrt(ln_total / s.trials)
            if ucb > best_score:
                best_arm, best_score = a, ucb
        return VariantChoice(arm_id=best_arm, reason="ucb1", score=best_score)
    except Exception as exc:  # noqa: BLE001 — never block a send on attribution
        _LOG.warning("select_variant errored (passthrough): %s", exc)
        return VariantChoice(arm_id=cands[0], reason=f"error_passthrough: {exc}")


def record_outcome(
    arm_id: str, reward: float, *, won: bool = False, now: float | None = None,
) -> None:
    """Learn from a variant outcome. Always accrues (even while disabled).
    Best-effort, never raises."""
    if not arm_id:
        return
    ts = time.time() if now is None else now
    persisted = False
    trials = 0
    wins = 0.0
    try:
        store = attr_store.get_store()
        stats = store.load(arm_id) or VariantStats(arm_id=arm_id)
        stats.trials += 1
        stats.reward_sum += max(0.0, min(1.0, float(reward)))
        if won:
            stats.wins += 1
        stats.last_updated = _iso(ts)
        store.save(stats)
        persisted = True
        trials, wins = int(stats.trials), float(stats.wins)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("record_outcome(%s) failed: %s", arm_id, exc)
    # Learning telemetry: the variant bandit emitted nothing on a successful
    # learn -- make it reconstructable (arm, reward, resulting trials/wins,
    # persisted). Additive; guarded so telemetry never blocks a send.
    try:
        from backend.common import learning_telemetry as _learning_telemetry
        _learning_telemetry.record_learning_update(
            kind=_learning_telemetry.KIND_VARIANT,
            arm_id=arm_id,
            outcome=float(reward),
            reward=float(reward),
            wins=wins,
            trials=trials,
            persisted=persisted,
            extra={"won": bool(won)},
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never block a send
        _LOG.debug("record_outcome learning telemetry skipped: %s", exc)


def snapshot(arm_id: str) -> dict:
    stats = attr_store.get_store().load(arm_id) or VariantStats(arm_id=arm_id)
    return {
        "enabled": _enabled(),
        "arm_id": arm_id,
        "trials": stats.trials,
        "wins": stats.wins,
        "mean_reward": round(stats.mean_reward, 4),
    }


__all__ = [
    "VariantChoice",
    "build_arm_id",
    "reward_from_signals",
    "select_variant",
    "record_outcome",
    "snapshot",
]
