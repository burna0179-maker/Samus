"""Persistent self-model / emotional-history tier (cognitive memory).

Ported from recovery/persona_memory_v2.py — the stranded ``PersonaMemory``
hardened self-model layer (WAL + crash-safe JSON persistence, recency-weighted
drift heuristic, stability score). Class name preserved; the module is renamed
to ``persona_self_model`` to avoid colliding with the live presentation-overlay
``backend/standard/persona/persona_manager.py`` (which is a display-name/avatar
concern, semantically unrelated to this cognitive tier).

Changes from the recovery original (additive / preservation-constrained):
  * Persistence path routes through ``backend.common.state_paths.state_path``
    (the writable-state-root resolver) rather than a caller-supplied raw path,
    so the WAL/JSON land on the ``samus-data`` volume in the container and under
    ``<code root>/state`` on the host — same convention the heat/attribution
    stores follow. ``SAMUS_PERSONA_SELF_MODEL_PATH`` overrides, mirroring
    ``SAMUS_ATTRIBUTION_PATH`` / ``SAMUS_HEAT_STATE_PATH``.
  * Gated by ``settings.persona_self_model_enabled`` (default OFF). When the
    flag is disabled the store constructs but never loads existing state and is
    a no-op on ``log_event`` / ``apply_introspection`` (dormant; no disk writes).

Wire-dormant: there is no live caller. This tier is available for the P2
``backend/cognitive/persona_frame.py`` compute-only consumer, which is itself
not attached to any live send/chat/outreach/cash_engine path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.common.config import get_settings
from backend.common.state_paths import state_path


SCHEMA_VERSION = "2.0"

# Default filename under the writable state root (mirrors the heat/attribution
# JSON-fallback convention). Override with SAMUS_PERSONA_SELF_MODEL_PATH.
_DEFAULT_STATE_PARTS = ("persona", "persona_self_model.json")


def _flag_enabled() -> bool:
    """Boot-time settings gate (default False). Mirrors the attribution flag."""
    return bool(getattr(get_settings(), "persona_self_model_enabled", False))


def default_self_model_path() -> str:
    """Resolve the persistent self-model JSON path.

    Honors ``SAMUS_PERSONA_SELF_MODEL_PATH``; otherwise routes through
    ``state_path`` so the file lands on the writable state volume in the
    container and under ``<code root>/state`` on the host. Returns a ``str``
    because :class:`PersonaMemory` accepts a path string.
    """
    override = os.getenv("SAMUS_PERSONA_SELF_MODEL_PATH", "").strip()
    if override:
        return override
    return str(state_path(*_DEFAULT_STATE_PARTS))


class PersonaMemory:
    def __init__(self, path: Optional[str] = None, max_events: int = 5000,
                 enabled: Optional[bool] = None,
                 wal_enabled: bool = True, async_flush: bool = True):
        # When ``enabled`` is not explicitly supplied, defer to the feature
        # flag (default OFF -> dormant). An explicit bool still wins so tests /
        # the P2 consumer can construct an active instance deterministically.
        self._path = Path(path if path is not None else default_self_model_path())
        self._wal_path = self._path.with_suffix(".wal.jsonl")
        self._max_events = int(max_events)
        self._enabled = _flag_enabled() if enabled is None else bool(enabled)
        self._wal_enabled = wal_enabled
        self._async_flush = async_flush
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._recent_index: deque = deque(maxlen=128)
        self._dirty = False
        self._last_flush = time.time()
        if self._enabled:
            self._events = self._load()
            self._rebuild_index()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text("utf-8"))
                if isinstance(data, dict) and "events" in data:
                    return data["events"]
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return self._recover_from_wal()

    def _recover_from_wal(self) -> List[Dict[str, Any]]:
        if not self._wal_path.exists():
            return []
        recovered = []
        try:
            with self._wal_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            recovered.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass
        return recovered

    def _save(self) -> None:
        try:
            payload = {"schema": SCHEMA_VERSION, "events": self._events, "ts": time.time()}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)
            self._dirty = False
            self._last_flush = time.time()
        except Exception:
            pass

    def _append_wal(self, evt: Dict[str, Any]) -> None:
        if not self._wal_enabled:
            return
        try:
            self._wal_path.parent.mkdir(parents=True, exist_ok=True)
            with self._wal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(evt) + "\n")
        except Exception:
            pass

    def _prune_if_needed(self) -> None:
        if len(self._events) <= self._max_events:
            return
        keep = int(self._max_events * 0.6)
        recent = self._events[-keep:]
        important = [e for e in self._events
                     if e.get("kind") == "reflection"
                     and "error" in str(e.get("details", {}).get("tags", {}))]
        self._events = (important + recent)[-self._max_events:]

    def log_event(self, kind: str, details: Dict[str, Any], ts: Optional[float] = None) -> None:
        if not self._enabled:
            return
        evt = {"ts": float(ts or time.time()), "kind": str(kind), "details": details}
        with self._lock:
            self._events.append(evt)
            self._recent_index.append(evt)
            self._append_wal(evt)
            self._prune_if_needed()
            self._dirty = True
        if not self._async_flush:
            self._save()
        elif self._dirty and (time.time() - self._last_flush) > 2.0:
            self._save()

    def latest_emotion(self) -> Dict[str, Any]:
        for rec in reversed(self._recent_index):
            if rec.get("kind") == "emotion":
                d = dict(rec.get("details") or {})
                d["ts"] = rec["ts"]
                return d
        return {"valence": 0.0, "confidence": 0.5, "novelty": 0.0}

    def _rebuild_index(self):
        for e in self._events[-128:]:
            self._recent_index.append(e)

    def heuristic_summary(self) -> Dict[str, Any]:
        drift_vals: List[float] = []
        weighted_drift = 0.0
        weight_sum = 0.0
        error_count = 0
        now = time.time()
        for rec in self._events[-500:]:
            if rec.get("kind") != "reflection":
                continue
            tags = rec.get("details", {}).get("tags", {})
            drift = float(tags.get("drift", 0.0))
            age = now - rec["ts"]
            weight = 1.0 / (1.0 + age / 300.0)
            weighted_drift += drift * weight
            weight_sum += weight
            if tags.get("status") == "error":
                error_count += 1
            drift_vals.append(drift)
        avg_drift = (weighted_drift / weight_sum) if weight_sum else 0.0
        return {
            "avg_drift": avg_drift,
            "raw_avg_drift": sum(drift_vals) / len(drift_vals) if drift_vals else 0.0,
            "error_rate": error_count / max(len(drift_vals), 1),
            "stability_score": max(0.0, 1.0 - avg_drift),
        }

    def _count_events_by_kind(self) -> Dict[str, int]:
        """Tally events by ``kind`` (cheap single pass over the buffer)."""
        emotion = 0
        reflection = 0
        for e in self._events:
            k = e.get("kind")
            if k == "emotion":
                emotion += 1
            elif k == "reflection":
                reflection += 1
        return {"emotion_events": emotion, "reflection_events": reflection}

    def snapshot(self) -> Dict[str, Any]:
        """Self-model snapshot. SUPERSET shape (Phase C — additive fix).

        Preserves every pre-existing key (``schema``/``emotion``/``heuristics``/
        ``event_count``/``ts``) AND every existing consumer. The build-plan Phase-C
        fix additively exposes the three keys ``backend.cognitive.persona_frame``'s
        ``_derive_frame`` reads off the snapshot but that the original shape did
        not surface, so persona-frame drift derivation no longer silently reads 0:

          * ``emotion.drift``            — the recency-weighted drift from
            ``heuristic_summary()['avg_drift']`` (frame previously fell back to
            ``1 - memory_confidence`` and, lacking that too, defaulted drift to 0).
          * ``emotion.memory_confidence`` — the recency-weighted stability
            (``heuristics.stability_score``) so the frame's secondary drift path
            also has a real signal.
          * ``baseline_confidence``      — the current emotional confidence the
            frame reads for its CAUTIOUS/RECOVERY confidence floors (frame read
            ``snap['baseline_confidence']`` which was absent -> defaulted 0.5).
          * ``counters``                 — ``{emotion_events, reflection_events}``
            so the frame's history-length gate (EXPLORE ``explore_min_history``)
            sees the real event tally instead of 0.

        The ``emotion`` sub-dict is COPIED before augmentation so ``latest_emotion``
        callers and the WAL records are untouched. No existing key is removed or
        changed in meaning — strictly a superset.
        """
        heur = self.heuristic_summary()
        emotion = dict(self.latest_emotion())  # copy: never mutate latest_emotion's source
        # Additive: surface drift + memory_confidence inside emotion (frame reads
        # emotion.drift first, then falls back to 1 - emotion.memory_confidence).
        emotion.setdefault("drift", float(heur.get("avg_drift", 0.0)))
        emotion.setdefault("memory_confidence", float(heur.get("stability_score", 0.5)))
        return {
            "schema": SCHEMA_VERSION,
            "emotion": emotion,
            "heuristics": heur,
            "event_count": len(self._events),
            "ts": time.time(),
            # --- additive superset keys persona_frame._derive_frame reads -----
            "baseline_confidence": float(emotion.get("confidence", 0.5)),
            "counters": self._count_events_by_kind(),
        }

    def apply_introspection(self, introspection: Dict[str, Any], *, plan_token=None,
                            user_text=None, success=None) -> Dict[str, Any]:
        drift = float(introspection.get("drift", 0.0))
        mem_conf = float(introspection.get("memory_confidence", 0.5))
        baseline = self.snapshot()["emotion"]["confidence"]
        new_conf = (baseline * 0.8) + (mem_conf * 0.2)
        valence = max(-1.0, min(1.0, (new_conf - 0.5) * 2.0 - drift * 0.5))
        self.log_event("emotion", {"valence": valence, "confidence": new_conf, "drift": drift})
        status = "error" if success is False else "struggle" if drift > 0.5 else "ok"
        self.log_event("reflection", {"text": f"[auto] drift={drift:.3f}",
                                      "tags": {"status": status, "plan_token": plan_token, "drift": drift}})
        return {"valence": valence, "confidence": new_conf, "drift": drift}

    def shutdown(self) -> None:
        with self._lock:
            if self._enabled:
                self._save()


__all__ = ["PersonaMemory", "SCHEMA_VERSION", "default_self_model_path"]
