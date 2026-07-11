"""Aggregate TranscriptAnalysis records into cross-call pattern intelligence.

Reads all analyses from <artifact_root>/voice/analyses/ and produces a
PatternReport with:

  - top objections by frequency + average handler effectiveness + best handler
  - conversion signals (prospect phrases that predicted positive outcomes)
  - "what to listen for" heuristics (aggregated from all calls)
  - talking point effectiveness (landed vs flopped frequency)
  - script element feedback summaries (opener, voicemail, pitch)
  - outcome distribution + positive rate

Writes PatternReport to <artifact_root>/voice/call_patterns.json.
callsheet_updater.py reads this to produce the dynamic callsheet intel.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.common import storage
from backend.common.dates import iso_now

from .transcript_analyzer import TranscriptAnalysis, load_all_analyses

_LOG = logging.getLogger("samus.voice.pattern_aggregator")

_PATTERNS_FILE = "voice/call_patterns.json"

_POSITIVE_OUTCOMES = frozenset({
    "converted",
    "warm_lead",
    "follow_up_scheduled",
    "call_back_requested",
})


@dataclass
class ObjectionPattern:
    text: str
    count: int
    avg_handling_effectiveness: float
    best_handler: str


@dataclass
class PatternReport:
    generated_ts: str
    total_calls_analyzed: int
    positive_outcome_rate: float
    avg_reward: float
    top_objections: list[ObjectionPattern] = field(default_factory=list)
    top_conversion_signals: list[str] = field(default_factory=list)
    what_to_listen_for: list[str] = field(default_factory=list)
    talking_points_landed: list[str] = field(default_factory=list)
    talking_points_flopped: list[str] = field(default_factory=list)
    script_feedback_summary: dict = field(default_factory=dict)
    outcome_distribution: dict = field(default_factory=dict)


def aggregate_patterns(analyses: list[TranscriptAnalysis] | None = None) -> PatternReport:
    """Build and persist a PatternReport from all known analyses.

    Pass ``analyses`` to skip disk load (useful in pipeline where analyses
    are already in memory). None means load from <artifact_root>/voice/analyses/.
    """
    ts = iso_now()

    if analyses is None:
        analyses = load_all_analyses()

    if not analyses:
        empty = PatternReport(
            generated_ts=ts,
            total_calls_analyzed=0,
            positive_outcome_rate=0.0,
            avg_reward=0.0,
        )
        _persist(empty)
        return empty

    total = len(analyses)
    positive = sum(1 for a in analyses if a.outcome in _POSITIVE_OUTCOMES)
    rewards = [a.reward for a in analyses]
    avg_reward = sum(rewards) / len(rewards) if rewards else 0.0

    # ── Objection patterns ─────────────────────────────────────────
    obj_counts: Counter[str] = Counter()
    obj_handlers: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for a in analyses:
        for obj in a.objections_hit:
            key = obj.objection_text.lower()[:120]
            obj_counts[key] += 1
            obj_handlers[key].append((obj.how_handled, obj.effectiveness_score))

    top_objections: list[ObjectionPattern] = []
    for text, count in obj_counts.most_common(12):
        handlers = obj_handlers[text]
        avg_eff = sum(s for _, s in handlers) / len(handlers) if handlers else 0.0
        best = max(handlers, key=lambda x: x[1], default=("", 0.0))[0]
        top_objections.append(ObjectionPattern(
            text=text,
            count=count,
            avg_handling_effectiveness=round(avg_eff, 3),
            best_handler=best[:300],
        ))

    # ── Conversion signals — weight by positive outcomes ───────────
    signals: list[str] = []
    for a in analyses:
        if a.outcome in _POSITIVE_OUTCOMES:
            signals.extend(a.conversion_signals)
    sig_counts: Counter[str] = Counter(s.lower()[:160] for s in signals if s)
    top_signals = [s for s, _ in sig_counts.most_common(15)]

    # ── What to listen for — aggregate all calls ───────────────────
    wtlf_all: list[str] = []
    for a in analyses:
        wtlf_all.extend(a.what_to_listen_for)
    wtlf_counts: Counter[str] = Counter(s.lower()[:160] for s in wtlf_all if s)
    top_wtlf = [s for s, _ in wtlf_counts.most_common(20)]

    # ── Talking point effectiveness ────────────────────────────────
    landed_all: list[str] = []
    flopped_all: list[str] = []
    for a in analyses:
        landed_all.extend(a.talking_points_landed)
        flopped_all.extend(a.talking_points_flopped)
    landed_counts: Counter[str] = Counter(s.lower()[:160] for s in landed_all if s)
    flopped_counts: Counter[str] = Counter(s.lower()[:160] for s in flopped_all if s)

    # ── Script feedback — concatenate last 5 per element ──────────
    feedback_parts: dict[str, list[str]] = defaultdict(list)
    for a in analyses:
        for key, val in (a.script_feedback or {}).items():
            if val:
                feedback_parts[key].append(str(val)[:300])
    script_summary = {k: " | ".join(v[-5:]) for k, v in feedback_parts.items()}

    # ── Outcome distribution ───────────────────────────────────────
    outcome_dist: Counter[str] = Counter(a.outcome for a in analyses)

    report = PatternReport(
        generated_ts=ts,
        total_calls_analyzed=total,
        positive_outcome_rate=round(positive / total, 3),
        avg_reward=round(avg_reward, 3),
        top_objections=top_objections,
        top_conversion_signals=top_signals,
        what_to_listen_for=top_wtlf,
        talking_points_landed=[s for s, _ in landed_counts.most_common(15)],
        talking_points_flopped=[s for s, _ in flopped_counts.most_common(15)],
        script_feedback_summary=script_summary,
        outcome_distribution=dict(outcome_dist),
    )
    _persist(report)
    return report


def _persist(report: PatternReport) -> None:
    try:
        target = storage.root() / _PATTERNS_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(report), indent=2, ensure_ascii=False, default=str)
        target.write_text(payload, encoding="utf-8")
        # Archive a dated snapshot for day-over-day trend analysis
        _archive_snapshot(target.parent, payload, report.generated_ts)
    except OSError as exc:
        _LOG.warning("pattern persist failed: %s", exc)


_SNAPSHOT_DIR = "voice/pattern_snapshots"


def _archive_snapshot(voice_dir: Path, payload: str, ts: str) -> None:
    """Write call_patterns_YYYY-MM-DD.json alongside the rolling file."""
    try:
        date_str = ts[:10]  # YYYY-MM-DD from ISO timestamp
        snap_dir = storage.root() / _SNAPSHOT_DIR
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_file = snap_dir / f"call_patterns_{date_str}.json"
        snap_file.write_text(payload, encoding="utf-8")
        _LOG.info("pattern_aggregator: archived snapshot → %s", snap_file.name)
    except OSError as exc:
        _LOG.warning("pattern snapshot archive failed: %s", exc)


def load_snapshot(date_str: str) -> PatternReport | None:
    """Load a dated snapshot (YYYY-MM-DD) if it exists."""
    snap_file = storage.root() / _SNAPSHOT_DIR / f"call_patterns_{date_str}.json"
    if not snap_file.exists():
        return None
    try:
        data = json.loads(snap_file.read_text(encoding="utf-8"))
        objs_raw = data.pop("top_objections", []) or []
        objs = [ObjectionPattern(**o) for o in objs_raw if isinstance(o, dict)]
        report = PatternReport(**data)
        report.top_objections = objs
        return report
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("load_snapshot(%s) failed: %s", date_str, exc)
        return None


def list_snapshots(limit: int = 14) -> list[str]:
    """Return up to ``limit`` most recent snapshot date strings, newest first."""
    snap_dir = storage.root() / _SNAPSHOT_DIR
    if not snap_dir.exists():
        return []
    dates = sorted(
        (p.stem.replace("call_patterns_", "") for p in snap_dir.glob("call_patterns_*.json")),
        reverse=True,
    )
    return dates[:limit]


def load_pattern_report() -> PatternReport | None:
    """Load the last persisted PatternReport, or None if unavailable."""
    target = storage.root() / _PATTERNS_FILE
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        objs_raw = data.pop("top_objections", []) or []
        objs = [ObjectionPattern(**o) for o in objs_raw if isinstance(o, dict)]
        report = PatternReport(**data)
        report.top_objections = objs
        return report
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("load_pattern_report failed: %s", exc)
        return None
