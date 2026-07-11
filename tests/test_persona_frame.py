"""PersonaSystem persona-frame — mode FSM, hysteresis/hold-timer, emergency
drift override, degraded fallback, disabled-noop, compute-only dormancy.

Mode derivation reads a duck-typed snapshot of the shape ``_derive_frame``
consumes (``emotion``/``baseline_confidence``/``counters``); a stub supplies it
so the FSM is driven deterministically. Flag flipped via env + reload_settings()
like tests/test_attribution.py.
"""

from __future__ import annotations


from backend.cognitive.persona_frame import (
    ModeThresholds,
    OperatingMode,
    PersonaSystem,
)


class _StubMemory:
    """Minimal persona-memory exposing the snapshot keys _derive_frame reads."""

    def __init__(
        self,
        *,
        valence=0.0,
        confidence=0.5,
        novelty=0.0,
        drift=0.0,
        emotion_events=0,
        reflection_events=0,
    ):
        self._snap = {
            "emotion": {"valence": valence, "novelty": novelty, "drift": drift},
            "baseline_confidence": confidence,
            "counters": {"emotion_events": emotion_events, "reflection_events": reflection_events},
        }

    def snapshot(self):
        return self._snap


def _set_flag(monkeypatch, enabled: bool):
    monkeypatch.setenv("SAMUS_PERSONA_FRAME_ENABLED", "true" if enabled else "false")
    from backend.common.settings import reload_settings

    reload_settings()


def _mode_of(enriched) -> str:
    return enriched[PersonaSystem.FRAME_KEY]["mode"]


# ---------------------------------------------------------------------------
# disabled-noop (default dormant / compute-only off)
# ---------------------------------------------------------------------------


def test_disabled_emits_neutral_degraded_frame(monkeypatch):
    _set_flag(monkeypatch, False)
    # enabled defaults to the flag (False).
    sys_ = PersonaSystem(persona_memory=_StubMemory(valence=-1.0, drift=1.0))
    out = sys_.transform({"task": "x"})
    frame = out[PersonaSystem.FRAME_KEY]
    assert frame["mode"] == OperatingMode.NORMAL.value
    assert frame["degraded"] is True
    assert "persona_system_disabled" in frame["flags"]
    # The original stimulus is preserved (deep-copied, not mutated).
    assert out["task"] == "x"


def test_disabled_does_not_derive_even_with_extreme_drift(monkeypatch):
    _set_flag(monkeypatch, False)
    sys_ = PersonaSystem(persona_memory=_StubMemory(drift=1.0), enabled=False)
    # Despite drift >= emergency ceiling, disabled short-circuits before derive.
    assert _mode_of(sys_.transform({})) == OperatingMode.NORMAL.value


# ---------------------------------------------------------------------------
# mode FSM transitions (explicit enabled to drive derivation)
# ---------------------------------------------------------------------------


def test_normal_baseline(monkeypatch):
    _set_flag(monkeypatch, True)
    sys_ = PersonaSystem(
        persona_memory=_StubMemory(valence=0.0, confidence=0.5, drift=0.1), enabled=True
    )
    assert _mode_of(sys_.transform({})) == OperatingMode.NORMAL.value


def test_explore_when_conditions_favorable(monkeypatch):
    _set_flag(monkeypatch, True)
    mem = _StubMemory(
        valence=0.5, confidence=0.85, novelty=0.1, drift=0.05, emotion_events=8, reflection_events=8
    )  # n_history=16 >= 10
    sys_ = PersonaSystem(persona_memory=mem, enabled=True)
    out = sys_.transform({})
    assert _mode_of(out) == OperatingMode.EXPLORE.value
    assert out[PersonaSystem.FRAME_KEY]["safety_posture"] == "expansive"


def test_cautious_on_high_drift(monkeypatch):
    _set_flag(monkeypatch, True)
    sys_ = PersonaSystem(persona_memory=_StubMemory(confidence=0.5, drift=0.7), enabled=True)
    out = sys_.transform({})
    assert _mode_of(out) == OperatingMode.CAUTIOUS.value
    assert out[PersonaSystem.FRAME_KEY]["safety_posture"] == "guarded"


def test_recovery_on_multi_signal(monkeypatch):
    _set_flag(monkeypatch, True)
    # low valence + low confidence = 2 recovery signals -> RECOVERY.
    sys_ = PersonaSystem(persona_memory=_StubMemory(valence=-0.5, confidence=0.2), enabled=True)
    out = sys_.transform({})
    assert _mode_of(out) == OperatingMode.RECOVERY.value
    assert out[PersonaSystem.FRAME_KEY]["safety_posture"] == "restrictive"


def test_recovery_on_error_streak(monkeypatch):
    _set_flag(monkeypatch, True)
    sys_ = PersonaSystem(persona_memory=_StubMemory(valence=0.0, confidence=0.6), enabled=True)
    for _ in range(3):  # recovery_consecutive_errors default = 3
        sys_.notify_cycle_outcome(success=False)
    assert _mode_of(sys_.transform({})) == OperatingMode.RECOVERY.value


# ---------------------------------------------------------------------------
# emergency drift override at >= 0.9
# ---------------------------------------------------------------------------


def test_emergency_drift_forces_recovery(monkeypatch):
    _set_flag(monkeypatch, True)
    # Otherwise-healthy signals, but drift at the emergency ceiling.
    sys_ = PersonaSystem(
        persona_memory=_StubMemory(valence=0.9, confidence=0.95, drift=0.95), enabled=True
    )
    out = sys_.transform({})
    assert _mode_of(out) == OperatingMode.RECOVERY.value
    assert any(f.startswith("extreme_drift") for f in out[PersonaSystem.FRAME_KEY]["flags"])


# ---------------------------------------------------------------------------
# hysteresis / hold timer (mode does not flap within the hold window)
# ---------------------------------------------------------------------------


def test_recovery_hold_timer_prevents_flap(monkeypatch):
    _set_flag(monkeypatch, True)
    t = ModeThresholds(mode_min_hold_seconds=9999.0)  # effectively infinite hold
    mem = _StubMemory(valence=-0.5, confidence=0.2)
    sys_ = PersonaSystem(persona_memory=mem, thresholds=t, enabled=True)
    assert _mode_of(sys_.transform({})) == OperatingMode.RECOVERY.value
    # Now feed a calm snapshot. Within the hold window the recovery mode holds
    # (single recovery signal would otherwise drop out of RECOVERY).
    sys_._memory = _StubMemory(valence=0.0, confidence=0.6, drift=0.1)
    held = sys_.transform({})
    assert _mode_of(held) == OperatingMode.RECOVERY.value
    assert "mode_hold:recovery" in held[PersonaSystem.FRAME_KEY]["flags"]


def test_cautious_hysteresis_on_lingering_drift(monkeypatch):
    _set_flag(monkeypatch, True)
    t = ModeThresholds(mode_min_hold_seconds=0.0)  # no hold; exercise drift hysteresis
    sys_ = PersonaSystem(
        persona_memory=_StubMemory(confidence=0.5, drift=0.7), thresholds=t, enabled=True
    )
    assert _mode_of(sys_.transform({})) == OperatingMode.CAUTIOUS.value
    # Drift drops below the cautious ceiling (0.6) but stays above the exit
    # floor (0.45) -> cautious hysteresis keeps it CAUTIOUS.
    sys_._memory = _StubMemory(confidence=0.6, valence=0.0, drift=0.5)
    out = sys_.transform({})
    assert _mode_of(out) == OperatingMode.CAUTIOUS.value
    assert "mode_hold:cautious" in out[PersonaSystem.FRAME_KEY]["flags"]


# ---------------------------------------------------------------------------
# degraded fallback (bad snapshot never crashes)
# ---------------------------------------------------------------------------


def test_degraded_on_bad_snapshot(monkeypatch):
    _set_flag(monkeypatch, True)

    class _BadMemory:
        def snapshot(self):
            return "not a dict"

    sys_ = PersonaSystem(persona_memory=_BadMemory(), enabled=True)
    out = sys_.transform({})
    frame = out[PersonaSystem.FRAME_KEY]
    assert frame["degraded"] is True
    assert frame["mode"] == OperatingMode.NORMAL.value
    assert any("persona_degraded" in f or f.startswith("error:") for f in frame["flags"])


def test_no_memory_degrades(monkeypatch):
    _set_flag(monkeypatch, True)
    sys_ = PersonaSystem(persona_memory=None, enabled=True)
    frame = sys_.transform({})[PersonaSystem.FRAME_KEY]
    assert frame["degraded"] is True
    assert "no_persona_memory" in frame["flags"]


# ---------------------------------------------------------------------------
# deterministic frame_id + snapshot shape
# ---------------------------------------------------------------------------


def test_frame_id_is_deterministic_and_snapshot_shape(monkeypatch):
    _set_flag(monkeypatch, True)
    sys_ = PersonaSystem(
        persona_memory=_StubMemory(valence=0.0, confidence=0.5, drift=0.1), enabled=True
    )
    out = sys_.transform({})
    fid = out[PersonaSystem.FRAME_KEY]["frame_id"]
    assert isinstance(fid, str) and len(fid) == 16
    snap = sys_.snapshot()
    assert set(snap) >= {
        "enabled",
        "mode",
        "consecutive_errors",
        "frame",
        "mode_history",
        "thresholds",
    }
    assert snap["enabled"] is True


# ---------------------------------------------------------------------------
# compute-only interop with the real P1 PersonaMemory (no crash, degraded-safe)
# ---------------------------------------------------------------------------


def test_compute_only_over_real_persona_memory(monkeypatch, tmp_path):
    _set_flag(monkeypatch, True)
    from backend.memory.persona_self_model import PersonaMemory

    mem = PersonaMemory(path=str(tmp_path / "m.json"), enabled=True, async_flush=False)
    mem.apply_introspection({"drift": 0.2, "memory_confidence": 0.8}, success=True)
    sys_ = PersonaSystem(persona_memory=mem, enabled=True)
    # PersonaMemory.snapshot() returns a dict (no 'counters'/'baseline_confidence'
    # keys); the FSM tolerates that via defaults and still produces a valid frame.
    frame = sys_.transform({"src": "interop"})[PersonaSystem.FRAME_KEY]
    assert frame["mode"] in {m.value for m in OperatingMode}
    assert isinstance(frame["frame_id"], str) and frame["frame_id"]
