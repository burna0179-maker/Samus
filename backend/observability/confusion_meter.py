"""ConfusionMeter — windowed aggregator over emitted ConfusionEvents.

`confusion_emitter.emit_confusion` writes one JSONL record per ConfusionEvent
(kr_gap / evidence_conflict / axiom_violation / goal_incoherence) into
`Samus/state/confusion/events.jsonl` (overridable via `SAMUS_CONFUSION_EVENTS_PATH`).

The meter reads the trailing window of those events and produces a normalized
ConfusionScore that other subsystems (EFH evaluator, PDC composite, autonomy
loop, portfolio_controller) can consume. The score is bounded in [0.0, 1.0]:

  0.0  — quiet window (no events, or only low-delta events)
  1.0  — saturated window (threshold_breach events + axiom_violation kind
         present, or high cumulative delta)

Pure-stdlib + JSON. No new env vars; window/path are caller-supplied with
sensible defaults aligned to confusion_emitter.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.common.state_paths import state_path

_KINDS = ("kr_gap", "evidence_conflict", "axiom_violation", "goal_incoherence")

# Kind-weights: how much each event-kind contributes per unit of delta.
# axiom_violation is the gravest; kr_gap (knowledge-representation gap) is
# the noisiest and weighted lightest.
_KIND_WEIGHT: dict[str, float] = {
    "kr_gap": 0.5,
    "evidence_conflict": 1.0,
    "goal_incoherence": 1.25,
    "axiom_violation": 2.0,
}

# Saturation point — cumulative weighted delta at which score hits 1.0.
# Tuned so that a single axiom_violation with delta=1.0 + threshold_breach
# saturates immediately (2.0 * 1.0 * 1.5 == 3.0 == saturation), while
# steady kr_gap noise (5 events at delta=0.2) yields ~0.17.
_SATURATION = 3.0

# threshold_breach amplifies the event's contribution.
_BREACH_AMPLIFIER = 1.5

# Default events.jsonl path mirrors confusion_emitter's default — under the
# writable state root (data volume in-container). SAMUS_CONFUSION_EVENTS_PATH
# still overrides via _events_path(). See backend/common/state_paths.py.
_DEFAULT_EVENTS_PATH = state_path("confusion", "events.jsonl")


@dataclass
class ConfusionScore:
    """Aggregated confusion state over a trailing window."""

    score: float                            # 0.0..1.0
    event_count: int
    breach_count: int                       # events with threshold_breach=True
    by_kind: dict[str, int] = field(default_factory=dict)
    weighted_delta: float = 0.0             # raw cumulative before normalization
    window_start: datetime | None = None
    window_end: datetime | None = None
    grade: str = "A"                        # A < 0.2, B < 0.4, C < 0.6, D < 0.8, F >= 0.8

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "event_count": self.event_count,
            "breach_count": self.breach_count,
            "by_kind": dict(self.by_kind),
            "weighted_delta": round(self.weighted_delta, 4),
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "grade": self.grade,
        }


def _events_path() -> Path:
    """Resolve the JSONL events path, honoring the env override."""
    return Path(os.environ.get("SAMUS_CONFUSION_EVENTS_PATH", str(_DEFAULT_EVENTS_PATH)))


def read_events(
    *,
    since: datetime | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read events from JSONL, optionally filtered to `ts >= since`.

    Malformed lines are skipped silently — this is a janitorial reader, not a
    validator. Events missing `ts` are kept (treated as in-window).
    """
    p = path or _events_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None and rec.get("ts"):
                try:
                    ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
                except ValueError:
                    out.append(rec)
                    continue
                if ts < since:
                    continue
            out.append(rec)
    return out


def _grade(score: float) -> str:
    if score < 0.2:
        return "A"
    if score < 0.4:
        return "B"
    if score < 0.6:
        return "C"
    if score < 0.8:
        return "D"
    return "F"


def compute_confusion_score(
    *,
    window_seconds: int = 3600,
    now: datetime | None = None,
    events: Iterable[dict[str, Any]] | None = None,
    path: Path | None = None,
) -> ConfusionScore:
    """Compute a windowed ConfusionScore.

    Args:
        window_seconds: trailing window. Default 1h.
        now: window end. Defaults to `datetime.now(UTC)`.
        events: pre-loaded events. If omitted, read from `path` (or env default).
        path: explicit JSONL path. Ignored if `events` is supplied.

    Returns:
        ConfusionScore. Score is clamped to [0,1] and grade is derived.

    Raises:
        ValueError: if window_seconds <= 0.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be > 0")

    window_end = now or datetime.now(timezone.utc)
    window_start = window_end - timedelta(seconds=window_seconds)

    if events is None:
        events = read_events(since=window_start, path=path)

    by_kind: dict[str, int] = {k: 0 for k in _KINDS}
    breach = 0
    weighted = 0.0
    total = 0
    for ev in events:
        kind = ev.get("kind")
        if kind not in _KIND_WEIGHT:
            continue
        delta = float(ev.get("delta", 0.0))
        if math.isnan(delta) or math.isinf(delta):
            continue
        delta = max(0.0, min(1.0, delta))
        amp = _BREACH_AMPLIFIER if ev.get("threshold_breach") else 1.0
        weighted += _KIND_WEIGHT[kind] * delta * amp
        by_kind[kind] += 1
        if ev.get("threshold_breach"):
            breach += 1
        total += 1

    score = min(1.0, weighted / _SATURATION)
    return ConfusionScore(
        score=score,
        event_count=total,
        breach_count=breach,
        by_kind=by_kind,
        weighted_delta=weighted,
        window_start=window_start,
        window_end=window_end,
        grade=_grade(score),
    )
