#!/usr/bin/env python3
"""
MetaCognitionEngine — autonomy wrapper around hardened CognitiveLoop
Source: ChatGPT recovery chat 35 (option B — wrapper, not replacement)

Canonical relationship:
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [WRAPPER PATTERN] sits ABOVE existing CognitiveLoop; does NOT modify it
- [EXPANDS §6 agents.cognitive] adds meta-perception (before) + meta-reflection (after)
- [EXPANDS §6 autonomy] situation indexing + heuristic bias + upgrade evaluation + autotuning
- [PRESERVES] async, container DI, Prometheus, compliance gates, memory controller

Key decision (chat 35): DO NOT replace hardened CognitiveLoop with CognitionManager.
                        CognitionManager lacks async, DI, observability, compliance enforcement.
                        Instead — wrap it. The wrapper is upgrade-aware; the loop is hardened.

Stage layering:
   META-PERCEPTION (situation index + heuristic pre-bias)
        ↓
   CognitiveLoop.cycle()  ← UNTOUCHED hardened pipeline
        ↓
   META-REFLECTION (autotune + upgrade-eval + architecture snapshot + reinforcement)

Integration shift (the ONLY change needed at the call site):
   # Before:
   loop = CognitiveLoop.from_settings()
   result = await loop.cycle(inp)

   # After:
   loop = CognitiveLoop.from_settings()
   meta = MetaCognitionEngine(loop)
   result = await meta.cycle(inp)
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


# Optional subsystems — fail-safe imports
try:
    from backend.autonomy.upgrade_engine import UpgradeEngine
except Exception:
    UpgradeEngine = None

try:
    from backend.autonomy.autotuner import Autotuner
except Exception:
    Autotuner = None

try:
    from backend.autonomy.situation_index import SituationIndex
except Exception:
    SituationIndex = None

try:
    from backend.autonomy.heuristic_registry import HeuristicRegistry
except Exception:
    HeuristicRegistry = None

try:
    from backend.autonomy.architecture_persistence import ArchitecturePersistence
except Exception:
    ArchitecturePersistence = None

try:
    from backend.autonomy.reinforcement_heuristics import ReinforcementHeuristicEngine
except Exception:
    ReinforcementHeuristicEngine = None


class MetaCognitionEngine:
    """Production-safe wrapper — adds autonomy without destabilizing the core loop."""

    def __init__(self, loop):
        self._loop = loop
        self._upgrade_engine = UpgradeEngine() if UpgradeEngine else None
        self._autotuner = Autotuner() if Autotuner else None
        self._situation = SituationIndex() if SituationIndex else None
        self._heuristics = HeuristicRegistry() if HeuristicRegistry else None
        self._persistence = ArchitecturePersistence() if ArchitecturePersistence else None
        self._reinforcement = ReinforcementHeuristicEngine() if ReinforcementHeuristicEngine else None

    async def cycle(self, inp):
        meta_start = time.perf_counter()

        # ----- META-PERCEPTION -----
        situation = self._index_situation(inp)
        bias = self._pre_heuristic_bias(inp, situation)
        if bias:
            inp.system_prompt += f"\n\nMeta-Heuristic Bias:\n{bias}"

        # ----- HARDENED CORE LOOP (UNTOUCHED) -----
        result = await self._loop.cycle(inp)

        # ----- META-REFLECTION -----
        self._post_cycle_analysis(inp, result, situation)

        meta_elapsed = round((time.perf_counter() - meta_start) * 1000, 2)
        return result

    # ----- meta stages -----
    def _index_situation(self, inp) -> Dict[str, Any]:
        if not self._situation:
            return {"risk": "unknown", "type": "generic"}
        try:
            return self._situation.classify(
                text=inp.user_text, channel=inp.channel, plan_token=inp.plan_token,
            )
        except Exception:
            return {"risk": "unknown", "type": "generic"}

    def _pre_heuristic_bias(self, inp, situation: Dict[str, Any]) -> str:
        if not self._heuristics:
            return ""
        try:
            base_bias = self._heuristics.evaluate_pre(text=inp.user_text, situation=situation) or ""
            if self._reinforcement:
                weight = self._reinforcement.get_weight(situation.get("type", "generic"))
                if base_bias:
                    return f"(weight={weight:.2f}) {base_bias}"
            return base_bias
        except Exception:
            return ""

    def _post_cycle_analysis(self, inp, result, situation: Dict[str, Any]) -> None:
        try:
            if self._autotuner:
                self._autotuner.adjust(
                    latency=result.elapsed_ms, errors=result.errors,
                    compliance_blocked=result.compliance_blocked,
                )
            if self._upgrade_engine:
                if self._upgrade_engine.evaluate(result=result, situation=situation):
                    self._upgrade_engine.execute()
            if self._persistence:
                self._persistence.snapshot(
                    plan_token=result.plan_token,
                    compliance_blocked=result.compliance_blocked,
                    error_count=len(result.errors),
                )
            # Reinforcement evolution (bounded, production-safe)
            if self._reinforcement:
                reward = 0.0
                if result.compliance_blocked:
                    reward -= 0.5
                if result.errors:
                    reward -= 0.3
                if not result.errors and not result.compliance_blocked:
                    reward += 0.4
                if result.elapsed_ms < 1500:
                    reward += 0.1
                reward = max(-1.0, min(1.0, reward))
                self._reinforcement.reinforce(situation.get("type", "generic"), reward)
        except Exception:
            pass


# -------------------------------------------------------------------
# ReinforcementHeuristicEngine — bounded RL for heuristic evolution
# -------------------------------------------------------------------
import time as _time
from dataclasses import dataclass, field
from typing import Dict as _Dict


@dataclass
class HeuristicProfile:
    weight: float = 1.0
    success_count: int = 0
    failure_count: int = 0
    decay: float = 0.995
    last_updated: float = field(default_factory=_time.time)


class ReinforcementHeuristicEngine:
    """
    Bounded reinforcement learning layer for heuristic evolution.
    Production safe: hard weight bounds, exponential decay, capped per-cycle updates.

    Why it matters:
      Each situation type ("command", "question", "high_risk", ...) evolves
      independently. Commands that consistently pass compliance + execute fast
      gain weight. High-risk patterns that get blocked or fail lose weight.

    Stability properties:
      - weight clamped to [MIN_WEIGHT, MAX_WEIGHT]
      - decay prevents runaway dominance
      - reward clamped to [-1.0, 1.0]
      - learning rate fixed
      - no unbounded self-modification
    """

    MIN_WEIGHT = 0.1
    MAX_WEIGHT = 5.0
    LEARNING_RATE = 0.05

    def __init__(self):
        self._heuristics: _Dict[str, HeuristicProfile] = {}

    def register(self, name: str) -> None:
        if name not in self._heuristics:
            self._heuristics[name] = HeuristicProfile()

    def get_weight(self, name: str) -> float:
        self.register(name)
        return self._heuristics[name].weight

    def reinforce(self, name: str, reward: float) -> None:
        """reward ∈ [-1.0, 1.0]"""
        self.register(name)
        p = self._heuristics[name]
        p.weight *= p.decay
        p.weight += reward * self.LEARNING_RATE
        p.weight = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, p.weight))
        if reward > 0:
            p.success_count += 1
        else:
            p.failure_count += 1
        p.last_updated = _time.time()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: {"weight": p.weight, "success": p.success_count, "failure": p.failure_count}
            for name, p in self._heuristics.items()
        }


# Further escalation options (chat 35 next-tier menu):
#   1. Reinforcement persistence across restarts
#   2. Multi-objective reward shaping
#   3. Adversarial detection penalty
#   4. Cryptographically signed heuristic registry
#   5. Heuristic tournament selection model
