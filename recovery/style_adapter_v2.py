#!/usr/bin/env python3
"""
StyleAdapter v2 — deterministic style translation module
Source: ChatGPT recovery chat 17

Canonical relationship:
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [EXPANDS §6 agents.cognitive] sits between SocialAdapter.decide() and prompt builder
- Pure mapping logic — NOT an agent, NOT autonomous
- [FIX] hf_style_adapter_enabled now actually enforced in decide()
- [NEW] input normalization + clamping pass
- [NEW] explicit plan-token classification (TASKY_PREFIXES, CHATTY_PREFIXES)
- [NEW] score-based tone selection (vs threshold snapping)
- [NEW] structured `trace` field on StyleDecision alongside `notes`
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))


def _safe_float(d, key, default):
    try:
        v = d.get(key, default)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _extract_valence(persona):
    if not persona: return 0.0
    emo = persona.get("emotion") or persona
    try: return _clamp(float(emo.get("valence", 0.0)), -1.0, 1.0)
    except (TypeError, ValueError): return 0.0


@dataclass
class StyleContext:
    user_text: str
    social: Dict[str, Any]
    persona: Optional[Dict[str, Any]] = None
    channel: str = "chat"
    plan_token: str = "unknown"
    dev_flags: Optional[Dict[str, Any]] = None


@dataclass
class StyleDecision:
    tone: str
    register: str
    target_length: str
    paragraph_density: float
    bullet_preference: float
    step_by_step: bool
    include_examples: bool
    include_disclaimers: bool
    soften_edges: bool
    tts_profile: Dict[str, float]
    notes: str
    trace: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self): return asdict(self)


class StyleAdapter:
    TASKY_PREFIXES = ("task.", "analysis.", "debug.", "code.", "ops.")
    CHATTY_PREFIXES = ("chat.", "voice.")
    DISCLAIMER_TOKENS = ("safety", "legal", "medical", "risk", "compliance")
    VALID_TONES = {"neutral", "warm", "supportive", "playful", "serious"}
    VALID_LENGTHS = {"short", "medium", "long", "auto"}

    def __init__(self, *, enabled=True):
        self._enabled = bool(enabled)

    def decide(self, ctx: StyleContext) -> StyleDecision:
        if not self._enabled:
            return self._default_decision("style_adapter_disabled_default",
                                          {"enabled": False, "plan_token": ctx.plan_token, "channel": ctx.channel})

        social = self._normalize(ctx.social or {})
        valence = _extract_valence(ctx.persona)
        plan_flags = self._classify_plan(ctx.plan_token)
        tone, tone_trace = self._resolve_tone(social, valence, ctx.dev_flags or {})
        register = self._resolve_register(social)
        target_length = self._resolve_length(social, ctx.dev_flags or {})
        paragraph_density, bullet_preference = self._resolve_structure(social, plan_flags)
        step_by_step = self._resolve_steps(social, plan_flags, ctx.plan_token)
        include_examples = social["empathy"] > 0.60 or social["clarification_bias"] > 0.50
        include_disclaimers = social["safety_override"] or any(m in (ctx.plan_token or "").lower()
                                                               for m in self.DISCLAIMER_TOKENS)
        soften_edges = (social["mode"] in ("supportive", "recovery")
                        or social["reassurance_level"] > 0.50 or valence < -0.50)
        tts = self._resolve_tts(social, valence)

        return StyleDecision(
            tone=tone, register=register, target_length=target_length,
            paragraph_density=paragraph_density, bullet_preference=bullet_preference,
            step_by_step=step_by_step, include_examples=include_examples,
            include_disclaimers=include_disclaimers, soften_edges=soften_edges,
            tts_profile=tts,
            notes=f"tone={tone} reg={register} len={target_length} mode={social['mode']}",
            trace={"enabled": True, "channel": ctx.channel, "plan_token": ctx.plan_token,
                   "plan_flags": plan_flags, "social": social, "persona_valence": valence,
                   "tone_resolution": tone_trace},
        )

    def _normalize(self, social):
        ed = str(social.get("explanation_depth", "normal")).strip().lower()
        if ed not in {"short", "normal", "deep"}: ed = "normal"
        return {
            "formality": _clamp(_safe_float(social, "formality", 0.5)),
            "warmth": _clamp(_safe_float(social, "warmth", 0.6)),
            "playfulness": _clamp(_safe_float(social, "playfulness", 0.3)),
            "directness": _clamp(_safe_float(social, "directness", 0.7)),
            "verbosity": _clamp(_safe_float(social, "verbosity", 0.6)),
            "empathy": _clamp(_safe_float(social, "empathy", 0.5)),
            "clarification_bias": _clamp(_safe_float(social, "clarification_bias", 0.3)),
            "reassurance_level": _clamp(_safe_float(social, "reassurance_level", 0.3)),
            "explanation_depth": ed,
            "allow_humor": bool(social.get("allow_humor", True)),
            "safety_override": bool(social.get("safety_override", False)),
            "humor_active": bool(social.get("humor_active", False)),
            "mode": str(social.get("mode", "normal")).strip().lower() or "normal",
        }

    def _classify_plan(self, plan_token):
        t = (plan_token or "").strip().lower()
        return {
            "is_tasky": t.startswith(self.TASKY_PREFIXES),
            "is_chatty": t.startswith(self.CHATTY_PREFIXES),
            "has_debug": "debug" in t,
            "has_code": "code" in t,
        }

    def _resolve_tone(self, social, valence, dev_flags):
        forced = dev_flags.get("force_tone")
        if forced and str(forced).strip().lower() in self.VALID_TONES:
            return str(forced).strip().lower(), {"forced": True}
        m = social["mode"]
        scores = {
            "supportive": (1.0 if m in ("supportive", "recovery") else 0.0) * 1.2
                          + social["empathy"] * 0.55 + social["reassurance_level"] * 0.45
                          + max(0.0, -valence) * 0.20,
            "playful": (1.0 if (m == "playful" and social["allow_humor"] and social["humor_active"]) else 0.0) * 1.25
                       + social["playfulness"] * 0.70 + max(0.0, valence) * 0.15,
            "serious": (1.0 if social["safety_override"] else 0.0) * 1.20
                       + (1.0 if m == "cautious" else 0.0) * 0.90
                       + social["formality"] * 0.55 + (1.0 - social["playfulness"]) * 0.15,
            "warm": social["warmth"] * 0.70 + social["empathy"] * 0.25 + max(0.0, valence) * 0.10,
            "neutral": 0.35 + (1.0 - abs(social["warmth"] - 0.5)) * 0.10,
        }
        priority = ["serious", "supportive", "warm", "playful", "neutral"]
        tone = max(priority, key=lambda k: (scores[k], -priority.index(k)))
        return tone, {"scores": {k: round(v, 4) for k, v in scores.items()}, "selected": tone}

    def _resolve_register(self, s):
        f = s["formality"]
        return "casual" if f <= 0.33 else "formal" if f >= 0.67 else "mixed"

    def _resolve_length(self, s, dev_flags):
        forced = dev_flags.get("force_length")
        if forced and str(forced).strip().lower() in self.VALID_LENGTHS:
            return str(forced).strip().lower()
        if s["explanation_depth"] == "short": return "short"
        if s["explanation_depth"] == "deep": return "long"
        v = s["verbosity"]
        return "short" if v <= 0.33 else "long" if v >= 0.67 else "medium"

    def _resolve_structure(self, s, pf):
        if pf["is_tasky"]:
            return _clamp(0.40 + s["empathy"] * 0.18 + s["verbosity"] * 0.12), _clamp(0.40 + s["directness"] * 0.35)
        if pf["is_chatty"]:
            return _clamp(0.52 + s["empathy"] * 0.20 + s["verbosity"] * 0.10), _clamp(0.15 + s["directness"] * 0.25)
        return _clamp(0.46 + s["empathy"] * 0.18 + s["verbosity"] * 0.10), _clamp(0.25 + s["directness"] * 0.28)

    def _resolve_steps(self, s, pf, plan_token):
        if s["explanation_depth"] == "short": return False
        t = (plan_token or "").lower()
        return pf["is_tasky"] or pf["has_debug"] or pf["has_code"] or "walkthrough" in t or "troubleshoot" in t

    def _resolve_tts(self, s, valence):
        energy = _clamp(0.50 + valence * 0.25 + s["playfulness"] * 0.20)
        if s["mode"] in ("recovery", "supportive"): energy = _clamp(energy - 0.15)
        return {"energy": energy, "warmth": _clamp(s["warmth"]), "pace": _clamp(0.45 + s["directness"] * 0.28)}

    def _default_decision(self, notes, trace=None):
        return StyleDecision(tone="neutral", register="mixed", target_length="medium",
                             paragraph_density=0.50, bullet_preference=0.30,
                             step_by_step=False, include_examples=False,
                             include_disclaimers=False, soften_edges=False,
                             tts_profile={"energy": 0.50, "warmth": 0.50, "pace": 0.50},
                             notes=notes, trace=trace or {})
