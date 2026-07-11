#!/usr/bin/env python3
"""
PersonaMemory v2 — Hardened persistent self-model layer
Source: ChatGPT recovery chat 18

Canonical relationship:
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [EXPANDS §6 context.tiers] long-term persona / emotional history
- [EXPANDS §6 data plane] WAL + crash-safe persistence
- [NEW] schema_version field (forward compat)
- [NEW] recency-weighted drift heuristic (not flat average)
- [NEW] WAL recovery path on corrupted JSON
- [NEW] stability_score for orchestration throttling

Operational role:
  Used by PersonaSystem v2 (_derive_frame reads memory.snapshot())
  Feeds: emotion.{valence, confidence, novelty, drift} + counters
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "2.0"


class PersonaMemory:
    def __init__(self, path: str, max_events: int = 5000, enabled: bool = True,
                 wal_enabled: bool = True, async_flush: bool = True):
        self._path = Path(path)
        self._wal_path = self._path.with_suffix(".wal.jsonl")
        self._max_events = int(max_events)
        self._enabled = enabled
        self._wal_enabled = wal_enabled
        self._async_flush = async_flush
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._recent_index: deque = deque(maxlen=128)
        self._dirty = False
        self._last_flush = time.time()
        if enabled:
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
        if not self._wal_path.exists(): return []
        recovered = []
        try:
            with self._wal_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try: recovered.append(json.loads(line))
                        except json.JSONDecodeError: continue
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
        if not self._wal_enabled: return
        try:
            with self._wal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(evt) + "\n")
        except Exception:
            pass

    def _prune_if_needed(self) -> None:
        if len(self._events) <= self._max_events: return
        keep = int(self._max_events * 0.6)
        recent = self._events[-keep:]
        important = [e for e in self._events
                     if e.get("kind") == "reflection"
                     and "error" in str(e.get("details", {}).get("tags", {}))]
        self._events = (important + recent)[-self._max_events:]

    def log_event(self, kind: str, details: Dict[str, Any], ts: Optional[float] = None) -> None:
        if not self._enabled: return
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
            if rec.get("kind") != "reflection": continue
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

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "emotion": self.latest_emotion(),
            "heuristics": self.heuristic_summary(),
            "event_count": len(self._events),
            "ts": time.time(),
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
