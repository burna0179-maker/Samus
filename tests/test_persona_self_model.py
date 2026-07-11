"""PersonaMemory self-model tier — WAL recovery, prune, atomic save, drift,
disabled-noop, state-root path resolution.

Mirrors tests/test_attribution.py: flag flipped via SAMUS_*_ENABLED env +
reload_settings(); paths supplied per-test so the suite is hermetic.
"""
from __future__ import annotations

import json

import pytest

from backend.memory import persona_self_model as psm
from backend.memory.persona_self_model import PersonaMemory


def _set_flag(monkeypatch, enabled: bool):
    monkeypatch.setenv("SAMUS_PERSONA_SELF_MODEL_ENABLED", "true" if enabled else "false")
    from backend.common.settings import reload_settings
    reload_settings()


# ---------------------------------------------------------------------------
# disabled-noop (default dormant)
# ---------------------------------------------------------------------------

def test_disabled_is_noop_no_disk_write(tmp_path, monkeypatch):
    _set_flag(monkeypatch, False)
    path = tmp_path / "persona" / "self_model.json"
    # enabled defaults to the flag (False) -> dormant.
    mem = PersonaMemory(path=str(path))
    mem.log_event("emotion", {"valence": 0.5, "confidence": 0.9})
    mem.apply_introspection({"drift": 0.3, "memory_confidence": 0.8}, success=True)
    mem.shutdown()
    # Nothing should have been written, and the snapshot is the neutral default.
    assert not path.exists()
    assert not path.with_suffix(".wal.jsonl").exists()
    snap = mem.snapshot()
    assert snap["event_count"] == 0
    # snapshot() is a SUPERSET (Phase-C additive fix): the neutral emotion still
    # carries the original valence/confidence/novelty AND now exposes the
    # drift/memory_confidence keys persona_frame reads. Assert the neutral values
    # rather than exact dict identity so the superset doesn't trip this dormancy
    # check. With zero events drift is 0.0 and stability (memory_confidence) 1.0.
    assert snap["emotion"]["valence"] == 0.0
    assert snap["emotion"]["confidence"] == 0.5
    assert snap["emotion"]["novelty"] == 0.0
    assert snap["emotion"]["drift"] == 0.0
    # Additive top-level superset keys are present + neutral when dormant.
    assert snap["baseline_confidence"] == 0.5
    assert snap["counters"] == {"emotion_events": 0, "reflection_events": 0}


def test_explicit_enabled_overrides_disabled_flag(tmp_path, monkeypatch):
    _set_flag(monkeypatch, False)  # flag OFF...
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), enabled=True, async_flush=False)  # ...explicit ON wins
    mem.log_event("emotion", {"valence": 0.2, "confidence": 0.7})
    assert path.exists()
    assert mem.snapshot()["event_count"] == 1


# ---------------------------------------------------------------------------
# atomic save + reload
# ---------------------------------------------------------------------------

def test_atomic_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), enabled=True, async_flush=False)
    mem.log_event("emotion", {"valence": 0.4, "confidence": 0.8, "novelty": 0.1})
    mem.log_event("reflection", {"text": "x", "tags": {"status": "ok", "drift": 0.2}})
    mem.shutdown()
    # No tmp file left behind (atomic replace) and the JSON is well-formed.
    assert not path.with_suffix(".json.tmp").exists()
    payload = json.loads(path.read_text("utf-8"))
    assert payload["schema"] == psm.SCHEMA_VERSION
    assert len(payload["events"]) == 2
    # A fresh instance reloads prior events.
    mem2 = PersonaMemory(path=str(path), enabled=True)
    assert mem2.snapshot()["event_count"] == 2
    assert mem2.latest_emotion()["valence"] == 0.4


# ---------------------------------------------------------------------------
# WAL recovery (corrupt primary JSON -> rebuild from WAL)
# ---------------------------------------------------------------------------

def test_wal_recovery_on_corrupt_primary(tmp_path):
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), enabled=True, async_flush=False)
    mem.log_event("emotion", {"valence": 0.6, "confidence": 0.9})
    mem.log_event("reflection", {"text": "y", "tags": {"status": "ok"}})
    # Corrupt the primary snapshot; the WAL still holds both events.
    path.write_text("{ this is not valid json", encoding="utf-8")
    assert path.with_suffix(".wal.jsonl").exists()
    recovered = PersonaMemory(path=str(path), enabled=True)
    assert recovered.snapshot()["event_count"] == 2


def test_wal_recovery_skips_corrupt_lines(tmp_path):
    path = tmp_path / "self_model.json"
    wal = path.with_suffix(".wal.jsonl")
    wal.parent.mkdir(parents=True, exist_ok=True)
    # One good line, one garbage line -> only the good event is recovered.
    wal.write_text(
        json.dumps({"ts": 1.0, "kind": "emotion", "details": {"valence": 0.1}}) + "\n"
        + "<<<corrupt>>>\n",
        encoding="utf-8",
    )
    mem = PersonaMemory(path=str(path), enabled=True)
    assert mem.snapshot()["event_count"] == 1


# ---------------------------------------------------------------------------
# prune (bounded event buffer; keep recent + important error reflections)
# ---------------------------------------------------------------------------

def test_prune_bounds_event_count(tmp_path):
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), max_events=50, enabled=True, async_flush=True)
    for i in range(200):
        mem.log_event("emotion", {"valence": 0.0, "confidence": 0.5, "i": i})
    assert mem.snapshot()["event_count"] <= 50


def test_prune_retains_error_reflections(tmp_path):
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), max_events=20, enabled=True, async_flush=True)
    # One important error reflection up front, then flood with plain events.
    mem.log_event("reflection", {"text": "boom", "tags": {"status": "error", "drift": 0.9}})
    for _ in range(100):
        mem.log_event("emotion", {"valence": 0.0, "confidence": 0.5})
    kept = [e for e in mem._events if e.get("kind") == "reflection"
            and "error" in str(e.get("details", {}).get("tags", {}))]
    assert kept, "important error reflection must survive prune"


# ---------------------------------------------------------------------------
# drift heuristic (recency-weighted) + apply_introspection
# ---------------------------------------------------------------------------

def test_drift_heuristic_recency_weighted(tmp_path):
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), enabled=True, async_flush=True)
    now = 1_000_000.0
    # Old low-drift reflection, recent high-drift reflection. Recency weighting
    # pulls avg_drift above the flat mean of the two.
    mem.log_event("reflection", {"text": "old", "tags": {"drift": 0.0}}, ts=now - 10_000)
    mem.log_event("reflection", {"text": "new", "tags": {"drift": 1.0}}, ts=now)
    h = mem.heuristic_summary()
    assert 0.0 <= h["avg_drift"] <= 1.0
    assert h["avg_drift"] > h["raw_avg_drift"]
    assert h["stability_score"] == pytest.approx(1.0 - h["avg_drift"])


def test_apply_introspection_logs_and_returns(tmp_path):
    path = tmp_path / "self_model.json"
    mem = PersonaMemory(path=str(path), enabled=True, async_flush=False)
    out = mem.apply_introspection({"drift": 0.7, "memory_confidence": 0.3}, success=False)
    assert set(out) == {"valence", "confidence", "drift"}
    assert -1.0 <= out["valence"] <= 1.0
    # An emotion + a reflection event were appended.
    assert mem.snapshot()["event_count"] == 2


# ---------------------------------------------------------------------------
# state-root path resolution (env override + state_paths fallback)
# ---------------------------------------------------------------------------

def test_path_env_override(monkeypatch, tmp_path):
    override = tmp_path / "override" / "model.json"
    monkeypatch.setenv("SAMUS_PERSONA_SELF_MODEL_PATH", str(override))
    assert psm.default_self_model_path() == str(override)


def test_path_falls_back_to_state_root(monkeypatch):
    monkeypatch.delenv("SAMUS_PERSONA_SELF_MODEL_PATH", raising=False)
    monkeypatch.setenv("SAMUS_STATE_ROOT", "/tmp/samus-state-test")
    resolved = psm.default_self_model_path()
    assert resolved.replace("\\", "/").startswith("/tmp/samus-state-test")
    assert resolved.endswith("persona_self_model.json")
