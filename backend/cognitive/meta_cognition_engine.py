#!/usr/bin/env python3
"""MetaCognitionEngine — autonomy wrapper around the hardened CognitiveLoop.

Native port of ``recovery/meta_cognition_engine.py`` (build-plan Phase D),
preservation-constrained — the recovery original is NOT edited; this is its
absorbed twin under ``backend/cognitive``. The wrapper sits ABOVE the
``CognitiveLoop`` (``backend/cognitive/cognitive_loop.py``) and does NOT modify
it: it adds META-PERCEPTION (situation index + heuristic pre-bias) before the
loop and META-REFLECTION (autotune + upgrade-eval + architecture snapshot +
reinforcement + persona notify) after it.

Stage layering (unchanged from recovery)::

   META-PERCEPTION (situation index + heuristic pre-bias [+ persona posture])
        v
   CognitiveLoop.cycle()  <- UNTOUCHED hardened pipeline
        v
   META-REFLECTION (autotune + upgrade-eval + snapshot + reinforce [+ persona notify])

Changes from the recovery original (additive / preservation-constrained):

  * **Dormancy flag** ``settings.autonomy_meta_enabled`` (default OFF; env
    ``SAMUS_AUTONOMY_META_ENABLED``). When OFF, :meth:`cycle` is a PURE
    PASSTHROUGH — it ``return await self._loop.cycle(inp)`` and runs NO meta
    stage at all (no situation index, no bias mutation of ``inp.system_prompt``,
    no reflection). The result is byte-identical to calling the wrapped loop with
    no wrapper, which is the build-plan's "disabled == direct loop.cycle"
    requirement. The six subsystems + persona are not even consulted.

  * **Subsystem source.** The six collaborators are imported from the Phase-B
    ``backend.autonomy.*`` package (where ``ReinforcementHeuristicEngine`` was
    ported), not the recovery file's inline copies. Each import is fail-safe and
    each construction tolerates ``None`` so the engine runs with subsystems
    absent (``MetaCognitionEngine(loop)`` with the autonomy package missing still
    works — every meta step degrades to a no-op).

  * **Stage-1 persona wiring (build-plan Phase D).** When the persona subsystem
    is importable, a ``PersonaSystem`` frame is derived in META-PERCEPTION and
    its ``safety_posture`` is folded into the pre-bias (alongside the
    HeuristicRegistry text + the RL weight), and ``notify_cycle_outcome`` is
    called in META-REFLECTION (alongside ``reinforcement.reinforce``). Persona is
    itself flag-gated + COMPUTE-ONLY, so when its flags are off it emits a
    disabled neutral frame and the posture fold is a no-op — additive and inert.

  * **Reward math is UNCHANGED** from recovery (compliance_blocked -0.5, errors
    -0.3, clean +0.4, fast<1500ms +0.1, clamped to [-1.0, 1.0]).

WIRING (updated 2026-07-06, Slice B): LIVE importer. ``backend/cognitive/runner.py``
imports ``MetaCognitionEngine`` at ``runner.py:246``, constructs it at
``runner.py:302``, and invokes ``rt.engine.cycle(inp)`` at ``runner.py:373`` on
every run. The Phase-F caller — the gateway route ``/api/samus/cognition/cycle``
plus the autonomous ``cognition_cadence`` tick loop started by the gateway
lifespan — reaches this wrapper. ``cognitive_loop_enabled`` and
``autonomy_meta_enabled`` both default ON as of Slice B; either can be turned
off at runtime via the env var to fall back to pure passthrough (byte-identical
to no wrapper).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


# Optional subsystems — fail-safe imports (mirrors the recovery original).
try:
    from backend.autonomy.upgrade_engine import UpgradeEngine
except Exception:  # pragma: no cover - import guard
    UpgradeEngine = None

try:
    from backend.autonomy.autotuner import Autotuner
except Exception:  # pragma: no cover - import guard
    Autotuner = None

try:
    from backend.autonomy.situation_index import SituationIndex
except Exception:  # pragma: no cover - import guard
    SituationIndex = None

try:
    from backend.autonomy.heuristic_registry import HeuristicRegistry
except Exception:  # pragma: no cover - import guard
    HeuristicRegistry = None

try:
    from backend.autonomy.architecture_persistence import ArchitecturePersistence
except Exception:  # pragma: no cover - import guard
    ArchitecturePersistence = None

try:
    from backend.autonomy.reinforcement_heuristics import ReinforcementHeuristicEngine
except Exception:  # pragma: no cover - import guard
    ReinforcementHeuristicEngine = None

# Stage-1 persona (additive Phase-D wiring) — fail-safe + flag-gated downstream.
try:
    from backend.cognitive.persona_frame import PersonaSystem
except Exception:  # pragma: no cover - import guard
    PersonaSystem = None


def _meta_enabled() -> bool:
    """Boot-time dormancy gate (default False).

    Mirrors the per-subsystem flag pattern. Any settings hiccup degrades to
    ``False`` (disabled / pure passthrough) — fail-safe toward dormancy.
    """
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "autonomy_meta_enabled", False))
    except Exception:  # pragma: no cover - never let settings arm/break the engine
        return False


class MetaCognitionEngine:
    """Production-safe wrapper — adds autonomy without destabilizing the core loop.

    Construct with the loop to wrap: ``MetaCognitionEngine(loop)``. All six
    autonomy collaborators + the persona system are optional; a missing import or
    a ``None`` instance makes the corresponding meta step a no-op. When the
    ``autonomy_meta_enabled`` flag is OFF (the default), :meth:`cycle` ignores all
    of them and delegates straight to ``loop.cycle`` (byte-identical passthrough).
    """

    def __init__(self, loop, *, enabled: Optional[bool] = None):
        self._loop = loop
        # Explicit bool wins (tests); otherwise sourced from the feature flag.
        self._enabled = _meta_enabled() if enabled is None else bool(enabled)
        self._upgrade_engine = UpgradeEngine() if UpgradeEngine else None
        self._autotuner = Autotuner() if Autotuner else None
        self._situation = SituationIndex() if SituationIndex else None
        self._heuristics = HeuristicRegistry() if HeuristicRegistry else None
        self._persistence = ArchitecturePersistence() if ArchitecturePersistence else None
        self._reinforcement = (
            ReinforcementHeuristicEngine() if ReinforcementHeuristicEngine else None
        )
        # Stage-1 persona system (compute-only, flag-gated). Constructed without a
        # persona memory: the frame degrades to a neutral safety posture when its
        # own flag is off / no memory is attached — additive + inert.
        self._persona = PersonaSystem() if PersonaSystem else None

    async def cycle(self, inp):
        # ----- DORMANCY: disabled == byte-identical to direct loop.cycle -----
        # When the flag is off the wrapper adds NOTHING: no situation index, no
        # bias mutation of inp.system_prompt, no meta-reflection. This is the
        # build-plan's "disabled == direct loop.cycle(inp)" requirement.
        if not self._enabled:
            return await self._loop.cycle(inp)

        meta_start = time.perf_counter()

        # ----- META-PERCEPTION -----
        situation = self._index_situation(inp)
        persona_frame = self._derive_persona_frame(inp)
        bias = self._pre_heuristic_bias(inp, situation, persona_frame)
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
                text=inp.user_text,
                channel=inp.channel,
                plan_token=inp.plan_token,
            )
        except Exception:
            return {"risk": "unknown", "type": "generic"}

    def _derive_persona_frame(self, inp) -> Optional[Dict[str, Any]]:
        """Derive the Stage-1 persona frame for this cycle (compute-only).

        Returns the frame dict (under ``PersonaSystem.FRAME_KEY``) or ``None`` when
        no persona system is wired / it errors. Never raises — persona is
        advisory; a derivation fault must not perturb the cycle.
        """
        if not self._persona or not hasattr(self._persona, "transform"):
            return None
        try:
            enriched = self._persona.transform(
                {
                    "user_text": getattr(inp, "user_text", "") or "",
                    "channel": getattr(inp, "channel", "") or "",
                }
            )
            if isinstance(enriched, dict):
                return enriched.get(getattr(self._persona, "FRAME_KEY", "_persona_frame"))
        except Exception:
            return None
        return None

    def _pre_heuristic_bias(
        self,
        inp,
        situation: Dict[str, Any],
        persona_frame: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Compose the advisory pre-bias string (recovery logic + persona fold).

        Recovery base: ``HeuristicRegistry.evaluate_pre`` text, prefixed with the
        RL weight for the situation type. Phase-D addition: when a persona frame
        is present, fold its ``safety_posture`` in as an additional advisory
        clause (alongside the HeuristicRegistry text + RL weight). Pure prompt
        text — it never bypasses a governance gate. Fail-safe to "".
        """
        if not self._heuristics:
            # Even without the HeuristicRegistry, persona posture alone can bias.
            return self._persona_posture_clause(persona_frame)
        try:
            base_bias = self._heuristics.evaluate_pre(text=inp.user_text, situation=situation) or ""
            if self._reinforcement and base_bias:
                weight = self._reinforcement.get_weight(situation.get("type", "generic"))
                base_bias = f"(weight={weight:.2f}) {base_bias}"
            posture = self._persona_posture_clause(persona_frame)
            if posture:
                return f"{base_bias} {posture}".strip() if base_bias else posture
            return base_bias
        except Exception:
            return ""

    @staticmethod
    def _persona_posture_clause(persona_frame: Optional[Dict[str, Any]]) -> str:
        """Advisory clause derived from the persona frame's safety posture.

        Empty when there is no frame, the posture is the neutral "balanced", OR
        the frame is degraded/disabled. The degraded/disabled gate is important:
        a dormant persona (its ``persona_frame_enabled`` flag default OFF, or no
        persona memory attached) emits a neutral frame with a fallback
        ``safety_posture="conservative"`` — folding THAT would inject a caution
        bias on every cycle purely because persona is off, which is not a real
        signal. So the posture only biases when persona is actually armed and
        producing a non-degraded frame (additive + inert when the flag is off).

        A restrictive/guarded/expansive posture from a live frame biases the
        prompt advisorily — consistent with the HeuristicRegistry's
        conservative-by-design bias text; it never bypasses a governance gate.
        """
        if not isinstance(persona_frame, dict):
            return ""
        # Dormant / degraded persona -> no fold (keeps the default a no-op).
        if persona_frame.get("degraded") or "persona_system_disabled" in (
            persona_frame.get("flags") or []
        ):
            return ""
        posture = str(persona_frame.get("safety_posture", "") or "")
        if not posture or posture == "balanced":
            return ""
        return f"(persona safety posture: {posture}) Bias toward caution accordingly."

    def _post_cycle_analysis(self, inp, result, situation: Dict[str, Any]) -> None:
        try:
            if self._autotuner:
                self._autotuner.adjust(
                    latency=result.elapsed_ms,
                    errors=result.errors,
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
            # Reinforcement evolution (bounded, production-safe). Reward math is
            # UNCHANGED from the recovery original.
            success = not result.errors and not result.compliance_blocked
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
            # Stage-1 persona outcome notification (build-plan Phase D) — alongside
            # the reinforcement step. ``introspection`` carries the drift/confidence
            # signal the persona self-model folds in (best-effort; persona's own
            # flag gates whether anything is persisted).
            if self._persona and hasattr(self._persona, "notify_cycle_outcome"):
                introspection = {
                    "drift": 0.0 if success else 0.5,
                    "memory_confidence": 1.0 if success else 0.4,
                    "plan_token": getattr(result, "plan_token", ""),
                }
                self._persona.notify_cycle_outcome(
                    success=success,
                    introspection=introspection,
                )
        except Exception:
            pass


__all__ = ["MetaCognitionEngine"]
