"""``ReinforcementHeuristicEngine`` — bounded RL for heuristic evolution (Contract 2).

Ported near-verbatim from ``recovery/meta_cognition_engine.py`` (the inline
``HeuristicProfile`` + ``ReinforcementHeuristicEngine``, recovery lines
168-227): bounded reinforcement learning where each situation type
("command", "question", "high_risk", ...) evolves an independent weight.

Stability properties (unchanged from the recovery original):
  * weight clamped to [MIN_WEIGHT=0.1, MAX_WEIGHT=5.0]
  * exponential decay (0.995) prevents runaway dominance
  * reward clamped to [-1.0, 1.0]
  * fixed learning rate (0.05)
  * no unbounded self-modification

Added on the port (build-plan Phase B):
  * **Persistence** under ``state_path("autonomy", "heuristic_weights.json")``
    so evolved weights survive a restart. Load-on-construct, save-on-reinforce.
    Fail-OPEN: any IO/JSON error degrades to in-memory-only — a persistence
    fault can never break a cycle.
  * **Flag gating** behind ``settings.autonomy_reinforcement_enabled``
    (default OFF). When OFF the engine is INERT: ``get_weight`` returns the
    neutral default weight (1.0), ``reinforce`` is a no-op (no learning, no
    disk write), and ``snapshot`` returns ``{}``. Behaviour is byte-identical
    to "engine absent" so the wrapper sees no effect until armed.

Contract 2 methods: ``get_weight(name) -> float``, ``reinforce(name, reward)
-> None``, ``snapshot()``.

DORMANCY: constructed no-arg; no live importer. Inert unless the flag is on
AND the wrapper (itself dormant) constructs it.
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Default weight handed back when the engine is disabled or a name is unknown
# and the engine is off (matches the recovery default HeuristicProfile.weight).
_DEFAULT_WEIGHT = 1.0


def _flag_enabled() -> bool:
    """Boot-time settings gate (default False). Mirrors the persona/attribution flags."""
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "autonomy_reinforcement_enabled", False))
    except Exception:  # pragma: no cover — never let a settings hiccup arm/break the engine
        return False


def _weights_path():
    """Resolve the persistence path under the writable state root, fail-safe."""
    from backend.common.state_paths import state_path

    return state_path("autonomy", "heuristic_weights.json")


@dataclass
class HeuristicProfile:
    """One situation type's evolving weight (ported from recovery)."""

    weight: float = 1.0
    success_count: int = 0
    failure_count: int = 0
    decay: float = 0.995
    last_updated: float = field(default_factory=_time.time)


class ReinforcementHeuristicEngine:
    """Bounded reinforcement layer for heuristic evolution. Flag-gated + persistent."""

    MIN_WEIGHT = 0.1
    MAX_WEIGHT = 5.0
    LEARNING_RATE = 0.05

    def __init__(self, *, enabled: Optional[bool] = None) -> None:
        # Explicit bool wins (tests); otherwise sourced from the feature flag.
        self._enabled = _flag_enabled() if enabled is None else bool(enabled)
        self._heuristics: Dict[str, HeuristicProfile] = {}
        if self._enabled:
            self._load()

    # ---- Contract-2 surface -------------------------------------------
    def register(self, name: str) -> None:
        if name not in self._heuristics:
            self._heuristics[name] = HeuristicProfile()

    def get_weight(self, name: str) -> float:
        """Return the current weight for ``name``.

        Disabled => the neutral default (1.0) with NO state mutation (so a
        disabled engine never even creates a profile). Enabled => register +
        return the live weight (matches the recovery contract).
        """
        if not self._enabled:
            return _DEFAULT_WEIGHT
        self.register(name)
        return self._heuristics[name].weight

    def reinforce(self, name: str, reward: float) -> None:
        """Apply a bounded reward update to ``name`` (reward ∈ [-1.0, 1.0]).

        Disabled => no-op (no learning, no disk write). Enabled => decay +
        reward step + clamp, exactly as the recovery original, then persist
        (fail-open). Never raises.
        """
        if not self._enabled:
            return
        try:
            self.register(name)
            reward = max(-1.0, min(1.0, float(reward)))
            p = self._heuristics[name]
            p.weight *= p.decay
            p.weight += reward * self.LEARNING_RATE
            p.weight = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, p.weight))
            if reward > 0:
                p.success_count += 1
            else:
                p.failure_count += 1
            p.last_updated = _time.time()
            self._save()
        except Exception:  # pragma: no cover — RL must never break a cycle
            log.exception("ReinforcementHeuristicEngine.reinforce failed for %s", name)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return per-heuristic stats. Empty dict when disabled."""
        if not self._enabled:
            return {}
        return {
            name: {
                "weight": p.weight,
                "success": p.success_count,
                "failure": p.failure_count,
            }
            for name, p in self._heuristics.items()
        }

    # ---- persistence (added on the port; fail-open) -------------------
    def _load(self) -> None:
        """Load persisted weights if present. Fail-open: errors -> empty state."""
        try:
            path = _weights_path()
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = raw.get("heuristics", {}) if isinstance(raw, dict) else {}
            for name, rec in data.items():
                if not isinstance(rec, dict):
                    continue
                self._heuristics[name] = HeuristicProfile(
                    weight=float(rec.get("weight", 1.0)),
                    success_count=int(rec.get("success_count", rec.get("success", 0))),
                    failure_count=int(rec.get("failure_count", rec.get("failure", 0))),
                    decay=float(rec.get("decay", 0.995)),
                    last_updated=float(rec.get("last_updated", _time.time())),
                )
        except Exception:  # pragma: no cover — load is best-effort
            log.exception("ReinforcementHeuristicEngine load failed; starting empty")
            self._heuristics = {}

    def _save(self) -> None:
        """Persist weights atomically. Fail-open: errors are logged + swallowed."""
        try:
            path = _weights_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "samus.autonomy.heuristic_weights/1",
                "ts": _time.time(),
                "heuristics": {
                    name: {
                        "weight": p.weight,
                        "success_count": p.success_count,
                        "failure_count": p.failure_count,
                        "decay": p.decay,
                        "last_updated": p.last_updated,
                    }
                    for name, p in self._heuristics.items()
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # pragma: no cover — save is best-effort
            log.exception("ReinforcementHeuristicEngine save failed")


__all__ = ["ReinforcementHeuristicEngine", "HeuristicProfile"]
