#!/usr/bin/env python3
"""
SocialAdapter v2.0 — PolicyEngine + DriftDetector + MemoryAdapter
Source: ChatGPT recovery chat 16 (frontier-grade SocialAdapter upgrade)

Canonical relationship:
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [EXPANDS §6 agents.cognitive] rhetoric/posture layer between RETRIEVE→ACT
- [NEW] PolicyEngine — ordered rule evaluation w/ priorities + immutable safety lock
- [NEW] DriftDetector — mode oscillation tracking (UCB-style window)
- [NEW] SocialMemoryAdapter — read-only adaptation from recent history
- [NEW] score-based mode resolution + hysteresis (vs branch-overwrite)
- [NEW] mode_confidence + drift_state + policy_hits in SocialDecision

3-layer stack:
  Layer 1: PolicyEngine — explicit rules with priority ordering
  Layer 2: DriftDetector — instability monitoring
  Layer 3: SocialMemoryAdapter — read-only history-driven bias

Hard guardrails:
  - safety_state in {warn, block} → mode locked to {cautious, recovery}
  - force_mode IGNORED when active mode is safety-critical
  - humor blocked when safety_override or mode in {recovery, cautious}
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, ClassVar, Deque, Dict, List, Optional, Tuple

_SAFETY_MODES = frozenset({"recovery", "cautious"})
_ALLOWED_SAFETY_STATES = frozenset({"ok", "warn", "block"})
_ALLOWED_CHANNELS = frozenset({"chat", "voice", "system"})
_MAX_EXEC_MS = 15.0


def _clamp01(v, d=0.0):
    try: return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError): return d


def _clamp11(v, d=0.0):
    try: return max(-1.0, min(1.0, float(v)))
    except (TypeError, ValueError): return d


def _safe_dict(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _stable_hash(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class SocialContext:
    user_text: str
    channel: str = "chat"
    plan_token: str = "unknown"
    safety_state: str = "ok"
    introspection: Optional[Dict[str, Any]] = None
    persona: Optional[Dict[str, Any]] = None
    dev_flags: Optional[Dict[str, Any]] = None
    affect: Optional[Dict[str, Any]] = None
    confidence: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.safety_state not in _ALLOWED_SAFETY_STATES:
            raise ValueError(f"Invalid safety_state: {self.safety_state!r}")
        if self.channel not in _ALLOWED_CHANNELS:
            self.channel = "chat"


@dataclass
class SocialDecision:
    formality: float = 0.5
    warmth: float = 0.6
    playfulness: float = 0.3
    directness: float = 0.6
    verbosity: float = 0.5
    safety_override: bool = False
    allow_humor: bool = True
    humor_safe: bool = True
    use_humor: bool = False
    empathy_focus: float = 0.5
    grounding_intensity: float = 0.3
    question_rate: float = 0.3
    meta_transparency: float = 0.2
    reassurance_level: float = 0.3
    challenge_level: float = 0.2
    clarification_bias: float = 0.3
    completion_bias: float = 0.7
    initiative: float = 0.5
    conveyance: float = 0.5
    mode: str = "normal"
    notes: List[str] = field(default_factory=list)
    trace_id: str = ""
    policy_hits: List[str] = field(default_factory=list)
    drift_score: float = 0.0
    drift_state: str = "stable"
    mode_confidence: float = 0.5
    adaptation_weight: float = 0.0
    source_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class PolicyResult:
    name: str
    priority: int
    mode: Optional[str] = None
    slider_overrides: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    block_humor: bool = False
    force_safety_override: Optional[bool] = None


@dataclass(frozen=True)
class PolicyRule:
    name: str
    priority: int
    evaluator: Callable[[SocialContext, Dict[str, Any]], Optional[PolicyResult]]


class PolicyEngine:
    def __init__(self, rules=None):
        self.rules = sorted(rules or [], key=lambda r: r.priority, reverse=True)

    def evaluate(self, ctx: SocialContext, signals: Dict[str, Any]) -> List[PolicyResult]:
        results = []
        for rule in self.rules:
            try:
                hit = rule.evaluator(ctx, signals)
                if hit is not None:
                    results.append(hit)
            except Exception:
                pass
        return sorted(results, key=lambda r: r.priority, reverse=True)


# --- Reference rules ---
def _rule_low_confidence_clarify(ctx, signals):
    conf = _safe_dict(ctx.confidence)
    if bool(conf.get("low", False)) or _clamp01(conf.get("uncertainty", 0.0)) > 0.7:
        return PolicyResult("low_confidence_clarify", 70,
                            slider_overrides={"clarification_bias": 0.8, "meta_transparency": 0.45,
                                              "question_rate": 0.55, "completion_bias": 0.45},
                            notes=["policy: increase clarification under low confidence"])
    return None


def _rule_high_stress_grounding(ctx, signals):
    if _clamp01(_safe_dict(ctx.introspection).get("stress", 0.0)) > 0.7:
        return PolicyResult("high_stress_grounding", 75,
                            mode="supportive" if ctx.safety_state == "ok" else None,
                            slider_overrides={"grounding_intensity": 0.8, "reassurance_level": 0.75,
                                              "empathy_focus": 0.85, "challenge_level": 0.05},
                            block_humor=True, notes=["policy: high stress → grounding"])
    return None


def _rule_system_channel_formal(ctx, signals):
    if ctx.channel == "system":
        return PolicyResult("system_channel_formal", 60,
                            slider_overrides={"formality": 0.85, "directness": 0.85, "warmth": 0.4, "playfulness": 0.0},
                            block_humor=True, notes=["policy: system channel → formal"])
    return None


def _rule_plan_execution_direct(ctx, signals):
    if any(x in (ctx.plan_token or "").lower() for x in ("execute", "apply", "repair", "patch", "deploy", "implement")):
        return PolicyResult("plan_execution_direct", 50,
                            slider_overrides={"directness": 0.9, "completion_bias": 0.85,
                                              "initiative": 0.75, "question_rate": 0.2},
                            notes=["policy: execution plans → direct completion"])
    return None


@dataclass
class DriftSnapshot:
    drift_score: float = 0.0
    drift_state: str = "stable"
    oscillating: bool = False
    dominant_mode: str = "normal"


class DriftDetector:
    def __init__(self, window_size=12):
        self.window_size = max(4, int(window_size))
        self._modes: Deque[str] = deque(maxlen=self.window_size)

    def observe(self, mode: str) -> DriftSnapshot:
        self._modes.append(mode)
        history = list(self._modes)
        if len(history) <= 1:
            return DriftSnapshot(0.0, "stable", False, mode)
        transitions = sum(1 for i in range(1, len(history)) if history[i] != history[i-1])
        score = transitions / max(1, len(history)-1)
        counts = {h: history.count(h) for h in set(history)}
        dominant = max(counts.items(), key=lambda kv: kv[1])[0]
        state = "unstable" if score > 0.7 else "drifting" if score > 0.35 else "stable"
        return DriftSnapshot(_clamp01(score), state, score > 0.55 and len(counts) >= 3, dominant)


@dataclass
class MemoryAdaptation:
    weight: float = 0.0
    dominant_mode: str = "normal"
    mode_distribution: Dict[str, float] = field(default_factory=dict)
    slider_bias: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


class SocialMemoryAdapter:
    _KNOWN_MODES = ("normal", "playful", "supportive", "cautious", "recovery")

    def adapt(self, ctx: SocialContext) -> MemoryAdaptation:
        persona = _safe_dict(ctx.persona)
        intro = _safe_dict(ctx.introspection)
        memory = _safe_dict(persona.get("social_memory") or intro.get("social_memory"))
        modes = memory.get("modes", [])
        slider_means = _safe_dict(memory.get("slider_means"))
        if not isinstance(modes, list) or not modes:
            return MemoryAdaptation()
        cleaned = [str(m) for m in modes if str(m) in self._KNOWN_MODES]
        if not cleaned:
            return MemoryAdaptation()
        counts = {m: cleaned.count(m) for m in set(cleaned)}
        total = len(cleaned)
        distribution = {m: c/total for m, c in counts.items()}
        dominant = max(distribution.items(), key=lambda kv: kv[1])[0]
        concentration = max(distribution.values())
        weight = _clamp01((concentration - 0.2) / 0.8)
        slider_bias = {k: _clamp01(v) for k, v in slider_means.items() if k in (
            "formality", "warmth", "playfulness", "directness", "verbosity",
            "empathy_focus", "grounding_intensity", "question_rate", "meta_transparency",
            "reassurance_level", "challenge_level", "clarification_bias",
            "completion_bias", "initiative", "conveyance",
        )}
        return MemoryAdaptation(weight=weight, dominant_mode=dominant,
                                mode_distribution=distribution, slider_bias=slider_bias,
                                notes=[f"dominant={dominant}", f"concentration={concentration:.2f}"])


# NOTE: SocialAdapter.decide() — full implementation in chat 16; abridged here for size.
# See: recovery chat 16 for the complete _resolve_mode_v2 + _stabilize_mode_with_drift +
# _compute_sliders + _resolve_humor methods.
