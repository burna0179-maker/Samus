"""Persona-frame stage — Stage 1 of the 9-stage CognitiveLoop.

Enriches incoming stimuli with persona context (valence, confidence, operating
mode) so all downstream stages are persona-aware. Computes OperatingMode from
recent emotional trajectory + drift signals. Provides a "lens" that colors
perception without altering the raw stimulus payload.

Target path: backend/standard/agents/cognitive/stages/persona_frame.py
Source recovery: persona_system_v2_frontier.py (chat 15)

Adaptations applied:
  - Stripped F:\\Samus `Component(component_id="...")` inheritance
  - Renamed `hf_persona_*` flags → `sn_persona_*`
  - Routed disk I/O through backend.core.infrastructure.filesystem.get_paths()
  - Added __plane__ marker
  - Added @mutation_scope (state-only; persona snapshot reads from memory tier)
  - Module-level singleton accessor (canonical pattern)
  - Decoupled from `emit_audit`; uses standard logging
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from backend.core.configuration.settings import get_settings
from backend.core.mutation.scope import mutation_scope, MutationType
from backend.core.protocols import HealthReport, HealthStatus

__plane__ = "agents"
__layer__ = "L3_agents"

_log = logging.getLogger("samus.agents.cognitive.persona_frame")


class OperatingMode(str, Enum):
    """Cognitive operating mode derived from persona state.

    NORMAL:   Baseline operation. Balanced risk tolerance.
    CAUTIOUS: Elevated drift, uncertainty, or recent instability.
    RECOVERY: Sustained negative signals or repeated failures.
    EXPLORE:  High confidence + low drift + positive valence.
    """
    NORMAL = "normal"
    CAUTIOUS = "cautious"
    RECOVERY = "recovery"
    EXPLORE = "explore"


@dataclass(frozen=True)
class PersonaFrame:
    """Immutable snapshot of persona state at stimulus-ingestion time."""
    mode: OperatingMode
    valence: float
    confidence: float
    novelty: float
    drift: float
    emotion_event_count: int
    reflection_count: int
    frame_ts: float
    flags: tuple[str, ...] = ()
    source: str = "persona_frame"
    schema_version: str = "2.0.0"
    degraded: bool = False
    decision_basis: tuple[str, ...] = ()
    safety_posture: str = "balanced"
    frame_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mode"] = self.mode.value
        d["flags"] = list(self.flags)
        d["decision_basis"] = list(self.decision_basis)
        return d


@dataclass
class ModeThresholds:
    recovery_valence_floor: float = -0.4
    recovery_confidence_floor: float = 0.25
    recovery_consecutive_errors: int = 3
    cautious_drift_ceiling: float = 0.6
    cautious_valence_floor: float = -0.15
    cautious_confidence_floor: float = 0.35
    explore_confidence_floor: float = 0.7
    explore_drift_ceiling: float = 0.15
    explore_valence_floor: float = 0.2
    explore_min_history: int = 10
    cautious_exit_drift_floor: float = 0.45
    mode_min_hold_seconds: float = 5.0
    emergency_drift_recovery_ceiling: float = 0.9


@mutation_scope(
    state_paths=("data/persona/**",),
    mutation_types={MutationType.PERSONA},
)
class PersonaFrameStage:
    """Stage 1 of the 9-stage CognitiveLoop.

    Settings driven:
        sn_persona_history_size                    — mode-history ring buffer
        sn_persona_emergency_drift_recovery_ceiling — auto-RECOVERY threshold
        sn_persona_mode_min_hold_seconds           — hysteresis hold timer

    Memory dependency:
        Injected via wire_persona_memory(). If not wired, frames degrade to
        a neutral PersonaFrame (degraded=True, safety_posture="conservative").
    """

    FRAME_KEY = "_persona_frame"
    plane_name = "agents:cognitive:persona_frame"

    def __init__(
        self,
        persona_memory: Any | None = None,
        thresholds: ModeThresholds | None = None,
    ) -> None:
        cfg = get_settings()
        self._memory = persona_memory
        self._thresholds = thresholds or ModeThresholds()
        self._history_size = max(8, int(getattr(cfg, "sn_persona_history_size", 32)))

        # Apply settings overrides to thresholds if present
        for attr in (
            "emergency_drift_recovery_ceiling",
            "mode_min_hold_seconds",
        ):
            setting_name = f"sn_persona_{attr}"
            if hasattr(cfg, setting_name):
                setattr(self._thresholds, attr, getattr(cfg, setting_name))

        self._consecutive_errors: int = 0
        self._last_frame: PersonaFrame | None = None
        self._last_mode_change_ts: float = 0.0
        self._mode_history: deque[dict] = deque(maxlen=self._history_size)
        self._lock = threading.RLock()

    def wire_persona_memory(self, persona_memory: Any) -> None:
        """Post-boot wiring: inject PersonaMemory after container boot."""
        with self._lock:
            self._memory = persona_memory

    async def transform(self, stimulus: dict) -> dict:
        """Async to match cognitive-stage protocol.

        Returns deep-copied stimulus with FRAME_KEY injected.
        On error: returns neutral frame with degraded=True. Never raises.
        """
        enriched = copy.deepcopy(stimulus)
        try:
            frame = self._derive_frame()
        except Exception as exc:
            _log.warning("persona_frame_derive_failed: %s", type(exc).__name__)
            frame = self._neutral_frame(
                flags=("persona_degraded", f"error:{type(exc).__name__}"),
                degraded=True,
                decision_basis=("transform_exception",),
            )
        enriched[self.FRAME_KEY] = frame.to_dict()
        with self._lock:
            self._last_frame = frame
        return enriched

    def notify_cycle_outcome(
        self,
        success: bool,
        decision: dict | None = None,
        introspection: dict | None = None,
    ) -> None:
        """Feed cycle outcome back into mode-derivation state."""
        with self._lock:
            self._consecutive_errors = 0 if success else self._consecutive_errors + 1
        if self._memory and introspection and hasattr(self._memory, "apply_introspection"):
            try:
                self._memory.apply_introspection(introspection, success=success)
            except Exception:
                pass

    @property
    def current_mode(self) -> OperatingMode:
        with self._lock:
            return self._last_frame.mode if self._last_frame else OperatingMode.NORMAL

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self.current_mode.value,
                "consecutive_errors": self._consecutive_errors,
                "frame": self._last_frame.to_dict() if self._last_frame else None,
                "mode_history": list(self._mode_history),
                "thresholds": asdict(self._thresholds),
            }

    def health(self) -> HealthReport:
        with self._lock:
            mode = self.current_mode
            degraded = self._last_frame.degraded if self._last_frame else True
        status = HealthStatus.OK
        if degraded:
            status = HealthStatus.DEGRADED
        elif mode == OperatingMode.RECOVERY:
            status = HealthStatus.DEGRADED
        return HealthReport(
            status=status,
            detail=f"mode={mode.value}, degraded={degraded}",
            metrics={
                "consecutive_errors": float(self._consecutive_errors),
                "history_size": float(len(self._mode_history)),
            },
        )

    # ── internals ──

    def _neutral_frame(
        self,
        *,
        flags: tuple[str, ...] = ("persona_unavailable",),
        degraded: bool = False,
        decision_basis: tuple[str, ...] = ("neutral_fallback",),
    ) -> PersonaFrame:
        return self._finalize_frame(PersonaFrame(
            mode=OperatingMode.NORMAL, valence=0.0, confidence=0.5,
            novelty=0.0, drift=0.0,
            emotion_event_count=0, reflection_count=0, frame_ts=time.time(),
            flags=flags, degraded=degraded, decision_basis=decision_basis,
            safety_posture="conservative" if degraded else "balanced",
        ))

    def _derive_frame(self) -> PersonaFrame:
        if not self._memory:
            return self._neutral_frame(flags=("no_persona_memory",), degraded=True)
        snap = self._memory.snapshot()
        if not isinstance(snap, dict):
            raise TypeError("PersonaMemory.snapshot() must return dict")
        emotion = snap.get("emotion") if isinstance(snap.get("emotion"), dict) else {}
        counters = snap.get("counters") if isinstance(snap.get("counters"), dict) else {}
        valence = self._clamp(float(emotion.get("valence", 0.0)), -1.0, 1.0)
        confidence = self._clamp(float(snap.get("baseline_confidence", 0.5)), 0.0, 1.0)
        novelty = self._clamp(float(emotion.get("novelty", 0.0)), 0.0, 1.0)
        drift = self._clamp(
            float(emotion.get("drift", 1.0 - float(emotion.get("memory_confidence", 0.5)))),
            0.0, 1.0,
        )
        n_emotions = int(counters.get("emotion_events", 0))
        n_reflections = int(counters.get("reflection_events", 0))
        mode, flags, basis = self._derive_mode(
            valence, confidence, drift, novelty, n_emotions + n_reflections,
        )
        return self._finalize_frame(PersonaFrame(
            mode=mode, valence=valence, confidence=confidence, novelty=novelty,
            drift=drift, emotion_event_count=n_emotions,
            reflection_count=n_reflections, frame_ts=time.time(),
            flags=tuple(flags), decision_basis=tuple(basis),
            safety_posture=self._infer_safety_posture(mode),
        ))

    def _derive_mode(
        self, valence: float, confidence: float, drift: float,
        novelty: float, n_history: int,
    ) -> tuple[OperatingMode, list[str], list[str]]:
        t = self._thresholds
        flags: list[str] = []
        basis: list[str] = []
        with self._lock:
            errors = self._consecutive_errors
            prev = self._last_frame.mode if self._last_frame else None
            within_hold = (
                prev is not None
                and (time.time() - self._last_mode_change_ts) < t.mode_min_hold_seconds
            )

        # Emergency RECOVERY override
        if drift >= t.emergency_drift_recovery_ceiling:
            return (OperatingMode.RECOVERY,
                    [f"extreme_drift:{drift:.2f}", "emergency_recovery"],
                    ["drift_exceeds_emergency_ceiling"])

        # RECOVERY
        rec: list[str] = []
        if errors >= t.recovery_consecutive_errors:
            rec.append(f"error_streak:{errors}")
        if valence <= t.recovery_valence_floor:
            rec.append(f"low_valence:{valence:.2f}")
        if confidence <= t.recovery_confidence_floor:
            rec.append(f"low_confidence:{confidence:.2f}")
        if len(rec) >= 2 or errors >= t.recovery_consecutive_errors:
            return OperatingMode.RECOVERY, rec + ["multi_signal_recovery"], ["recovery_triggered"]
        if prev == OperatingMode.RECOVERY and within_hold:
            return OperatingMode.RECOVERY, ["mode_hold:recovery"], ["hold_timer_active"]

        # CAUTIOUS
        caut: list[str] = []
        if drift >= t.cautious_drift_ceiling:
            caut.append(f"high_drift:{drift:.2f}")
        if valence <= t.cautious_valence_floor:
            caut.append(f"neg_valence:{valence:.2f}")
        if confidence <= t.cautious_confidence_floor:
            caut.append(f"low_conf:{confidence:.2f}")
        if errors > 0:
            caut.append(f"recent_errors:{errors}")
        if novelty >= 0.85 and confidence < max(t.explore_confidence_floor, 0.75):
            caut.append(f"high_novelty_low_cert:{novelty:.2f}")
        if caut:
            return OperatingMode.CAUTIOUS, caut + ["caution_signals"], ["cautious_signal_present"]
        if prev == OperatingMode.CAUTIOUS and drift >= t.cautious_exit_drift_floor:
            return (OperatingMode.CAUTIOUS,
                    [f"lingering_drift:{drift:.2f}", "mode_hold:cautious"],
                    ["cautious_hysteresis"])

        # EXPLORE
        if (confidence >= t.explore_confidence_floor
                and drift <= t.explore_drift_ceiling
                and valence >= t.explore_valence_floor
                and n_history >= t.explore_min_history
                and errors == 0):
            return (OperatingMode.EXPLORE,
                    ["conditions_favorable"],
                    ["high_confidence", "low_drift", "positive_valence", "sufficient_history"])

        return OperatingMode.NORMAL, ["mode_trigger:default"], ["default_baseline"]

    def _finalize_frame(self, frame: PersonaFrame) -> PersonaFrame:
        frame_id = self._build_frame_id(frame)
        finalized = PersonaFrame(
            mode=frame.mode,
            valence=self._clamp(frame.valence, -1.0, 1.0),
            confidence=self._clamp(frame.confidence, 0.0, 1.0),
            novelty=self._clamp(frame.novelty, 0.0, 1.0),
            drift=self._clamp(frame.drift, 0.0, 1.0),
            emotion_event_count=max(0, int(frame.emotion_event_count)),
            reflection_count=max(0, int(frame.reflection_count)),
            frame_ts=float(frame.frame_ts),
            flags=tuple(self._dedupe(list(frame.flags))),
            source=frame.source,
            schema_version=frame.schema_version,
            degraded=bool(frame.degraded),
            decision_basis=tuple(self._dedupe(list(frame.decision_basis))),
            safety_posture=frame.safety_posture,
            frame_id=frame_id,
        )
        with self._lock:
            prev_mode = self._last_frame.mode if self._last_frame else None
            if prev_mode != finalized.mode:
                self._last_mode_change_ts = finalized.frame_ts
            self._mode_history.append({
                "ts": finalized.frame_ts, "frame_id": finalized.frame_id,
                "mode": finalized.mode.value,
                "previous_mode": prev_mode.value if prev_mode else None,
                "degraded": finalized.degraded, "flags": list(finalized.flags[:8]),
            })
        return finalized

    def _infer_safety_posture(self, mode: OperatingMode) -> str:
        return {
            "recovery": "restrictive",
            "cautious": "guarded",
            "explore": "expansive",
            "normal": "balanced",
        }.get(mode.value, "balanced")

    def _build_frame_id(self, frame: PersonaFrame) -> str:
        payload = {
            "mode": frame.mode.value,
            "valence": round(frame.valence, 6),
            "confidence": round(frame.confidence, 6),
            "drift": round(frame.drift, 6),
            "frame_ts": round(frame.frame_ts, 6),
            "flags": list(frame.flags),
            "decision_basis": list(frame.decision_basis),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, float(v)))

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out


_instance: PersonaFrameStage | None = None


def get_persona_frame() -> PersonaFrameStage:
    """Module-level singleton accessor — matches canonical pattern."""
    global _instance
    if _instance is None:
        _instance = PersonaFrameStage()
    return _instance
