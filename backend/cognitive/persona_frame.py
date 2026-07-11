"""Stage-1 persona-frame derivation — operating-mode FSM (compute-only).

Ported from recovery/persona_system_v2_frontier.py — the stranded frontier-grade
``PersonaSystem`` (+ ``OperatingMode`` / ``PersonaFrame`` / ``ModeThresholds``).
Class/enum/dataclass names preserved; the module lives in the new
``backend/cognitive`` package to avoid colliding with the live presentation
overlay ``backend/standard/persona/persona_manager.py``.

It derives a deterministic, thread-safe operating-mode frame (NORMAL / CAUTIOUS /
RECOVERY / EXPLORE) from the emotional + drift state of a duck-typed persona
self-model (anything exposing ``.snapshot()`` / ``.apply_introspection()`` — e.g.
``backend.memory.persona_self_model.PersonaMemory``). Features: emergency
RECOVERY override at drift >= ceiling, mode hysteresis + hold timer, sha256
frame_id, safety-posture inference, and safe degradation to a neutral frame on
any bad snapshot.

Changes from the recovery original (additive / preservation-constrained):
  * Gated by ``settings.persona_frame_enabled`` (default OFF). When the flag is
    disabled, :meth:`PersonaSystem.transform` emits a degraded, disabled neutral
    frame and performs no derivation — identical fail-closed shape to the
    original ``enabled=False`` construction path.
  * Removed nothing; only the ``enabled`` default is sourced from the flag when
    not explicitly supplied.

COMPUTE-ONLY + wire-dormant: there is NO live consumer. ``transform`` /
``snapshot`` may be called over a P1 ``PersonaMemory``, but the result is not
attached to any live send/chat/outreach/cash_engine path. Attaching a frame to
real behaviour is explicitly deferred to an operator decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

from backend.common.config import get_settings


def _flag_enabled() -> bool:
    """Boot-time settings gate (default False). Mirrors the attribution flag."""
    return bool(getattr(get_settings(), "persona_frame_enabled", False))


class OperatingMode(str, Enum):
    NORMAL = "normal"
    CAUTIOUS = "cautious"
    RECOVERY = "recovery"
    EXPLORE = "explore"


@dataclass(frozen=True)
class PersonaFrame:
    mode: OperatingMode
    valence: float
    confidence: float
    novelty: float
    drift: float
    emotion_event_count: int
    reflection_count: int
    frame_ts: float
    flags: List[str] = field(default_factory=list)
    source: str = "persona_system"
    schema_version: str = "2.0.0"
    degraded: bool = False
    decision_basis: List[str] = field(default_factory=list)
    safety_posture: str = "balanced"
    frame_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mode"] = self.mode.value
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
    recovery_exit_error_streak_max: int = 0
    cautious_exit_drift_floor: float = 0.45
    mode_min_hold_seconds: float = 5.0
    emergency_drift_recovery_ceiling: float = 0.9


class PersonaSystem:
    """Frontier-grade Stage 1 — deterministic, thread-safe, fail-closed."""

    FRAME_KEY = "_persona_frame"
    DEFAULT_HISTORY_SIZE = 32

    def __init__(
        self,
        persona_memory=None,
        thresholds=None,
        enabled: Optional[bool] = None,
        audit_enabled=True,
        history_size=DEFAULT_HISTORY_SIZE,
    ):
        self._memory = persona_memory
        self._thresholds = thresholds or ModeThresholds()
        # Default the enable from the feature flag (default OFF -> compute-only
        # disabled neutral frame). An explicit bool still wins for tests / the
        # eventual operator-gated consumer.
        self._enabled = _flag_enabled() if enabled is None else bool(enabled)
        self._audit_enabled = bool(audit_enabled)
        self._history_size = max(8, int(history_size))
        self._consecutive_errors = 0
        self._last_frame: Optional[PersonaFrame] = None
        self._last_mode_change_ts = 0.0
        self._mode_history: Deque[Dict[str, Any]] = deque(maxlen=self._history_size)
        self._lock = threading.RLock()

    def transform(self, stimulus: Dict[str, Any]) -> Dict[str, Any]:
        enriched = copy.deepcopy(stimulus)
        if not self._enabled:
            frame = self._neutral_frame(
                flags=["persona_system_disabled"],
                degraded=True,
                decision_basis=["feature_flag_disabled"],
            )
            enriched[self.FRAME_KEY] = frame.to_dict()
            with self._lock:
                self._last_frame = frame
            return enriched
        try:
            frame = self._derive_frame()
            enriched[self.FRAME_KEY] = frame.to_dict()
            with self._lock:
                self._last_frame = frame
            return enriched
        except Exception as exc:
            frame = self._neutral_frame(
                flags=["persona_degraded", f"error:{type(exc).__name__}"],
                degraded=True,
                decision_basis=["transform_exception"],
            )
            enriched[self.FRAME_KEY] = frame.to_dict()
            with self._lock:
                self._last_frame = frame
            return enriched

    def notify_cycle_outcome(self, success: bool, decision=None, introspection=None) -> None:
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

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "mode": self.current_mode.value,
                "consecutive_errors": self._consecutive_errors,
                "frame": self._last_frame.to_dict() if self._last_frame else None,
                "mode_history": list(self._mode_history),
                "thresholds": asdict(self._thresholds),
            }

    # --- internals ---
    def _neutral_frame(self, *, flags=None, degraded=False, decision_basis=None) -> PersonaFrame:
        return self._finalize_frame(
            PersonaFrame(
                mode=OperatingMode.NORMAL,
                valence=0.0,
                confidence=0.5,
                novelty=0.0,
                drift=0.0,
                emotion_event_count=0,
                reflection_count=0,
                frame_ts=time.time(),
                flags=list(flags or ["persona_unavailable"]),
                degraded=degraded,
                decision_basis=list(decision_basis or ["neutral_fallback"]),
                safety_posture="conservative" if degraded else "balanced",
            )
        )

    def _derive_frame(self) -> PersonaFrame:
        if not self._memory:
            return self._neutral_frame(flags=["no_persona_memory"], degraded=True)
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
            0.0,
            1.0,
        )
        n_emotions = int(counters.get("emotion_events", 0))
        n_reflections = int(counters.get("reflection_events", 0))
        mode, flags, basis = self._derive_mode(
            valence, confidence, drift, novelty, n_emotions + n_reflections
        )
        return self._finalize_frame(
            PersonaFrame(
                mode=mode,
                valence=valence,
                confidence=confidence,
                novelty=novelty,
                drift=drift,
                emotion_event_count=n_emotions,
                reflection_count=n_reflections,
                frame_ts=time.time(),
                flags=flags,
                decision_basis=basis,
                safety_posture=self._infer_safety_posture(mode),
            )
        )

    def _derive_mode(
        self, valence, confidence, drift, novelty, n_history
    ) -> Tuple[OperatingMode, List[str], List[str]]:
        t = self._thresholds
        flags, basis = [], []
        with self._lock:
            error_streak = self._consecutive_errors
            previous_mode = self._last_frame.mode if self._last_frame else None
            within_hold = (
                previous_mode is not None
                and (time.time() - self._last_mode_change_ts) < t.mode_min_hold_seconds
            )

        # Emergency override
        if drift >= t.emergency_drift_recovery_ceiling:
            return (
                OperatingMode.RECOVERY,
                [f"extreme_drift:{drift:.2f}", "emergency_recovery"],
                ["drift_exceeds_emergency_ceiling"],
            )

        # Recovery
        rec_reasons = []
        if error_streak >= t.recovery_consecutive_errors:
            rec_reasons.append(f"error_streak:{error_streak}")
        if valence <= t.recovery_valence_floor:
            rec_reasons.append(f"low_valence:{valence:.2f}")
        if confidence <= t.recovery_confidence_floor:
            rec_reasons.append(f"low_confidence:{confidence:.2f}")
        if len(rec_reasons) >= 2 or error_streak >= t.recovery_consecutive_errors:
            return (
                OperatingMode.RECOVERY,
                rec_reasons + ["multi_signal_recovery"],
                ["recovery_triggered"],
            )

        # Hysteresis hold on recovery
        if previous_mode == OperatingMode.RECOVERY and within_hold:
            return OperatingMode.RECOVERY, ["mode_hold:recovery"], ["hold_timer_active"]

        # Cautious
        caut_reasons = []
        if drift >= t.cautious_drift_ceiling:
            caut_reasons.append(f"high_drift:{drift:.2f}")
        if valence <= t.cautious_valence_floor:
            caut_reasons.append(f"neg_valence:{valence:.2f}")
        if confidence <= t.cautious_confidence_floor:
            caut_reasons.append(f"low_conf:{confidence:.2f}")
        if error_streak > 0:
            caut_reasons.append(f"recent_errors:{error_streak}")
        if novelty >= 0.85 and confidence < max(t.explore_confidence_floor, 0.75):
            caut_reasons.append(f"high_novelty_low_cert:{novelty:.2f}")
        if caut_reasons:
            return (
                OperatingMode.CAUTIOUS,
                caut_reasons + ["caution_signals"],
                ["cautious_signal_present"],
            )
        if previous_mode == OperatingMode.CAUTIOUS and drift >= t.cautious_exit_drift_floor:
            return (
                OperatingMode.CAUTIOUS,
                [f"lingering_drift:{drift:.2f}", "mode_hold:cautious"],
                ["cautious_hysteresis"],
            )

        # Explore
        if (
            confidence >= t.explore_confidence_floor
            and drift <= t.explore_drift_ceiling
            and valence >= t.explore_valence_floor
            and n_history >= t.explore_min_history
            and error_streak == 0
        ):
            return (
                OperatingMode.EXPLORE,
                ["conditions_favorable"],
                ["high_confidence", "low_drift", "positive_valence", "sufficient_history"],
            )

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
            flags=self._dedupe(frame.flags),
            source=frame.source,
            schema_version=frame.schema_version,
            degraded=bool(frame.degraded),
            decision_basis=self._dedupe(frame.decision_basis),
            safety_posture=frame.safety_posture,
            frame_id=frame_id,
        )
        with self._lock:
            previous_mode = self._last_frame.mode if self._last_frame else None
            if previous_mode != finalized.mode:
                self._last_mode_change_ts = finalized.frame_ts
            self._mode_history.append(
                {
                    "ts": finalized.frame_ts,
                    "frame_id": finalized.frame_id,
                    "mode": finalized.mode.value,
                    "previous_mode": previous_mode.value if previous_mode else None,
                    "degraded": finalized.degraded,
                    "flags": finalized.flags[:8],
                }
            )
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
            "flags": frame.flags,
            "decision_basis": frame.decision_basis,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(hi, float(v)))

    @staticmethod
    def _dedupe(items):
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out


__all__ = ["OperatingMode", "PersonaFrame", "ModeThresholds", "PersonaSystem"]
