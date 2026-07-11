"""Tests for backend.observability.confusion_meter."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.observability.confusion_meter import (
    ConfusionScore,
    compute_confusion_score,
    read_events,
)


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def test_empty_window_returns_zero_score(tmp_path):
    s = compute_confusion_score(events=[])
    assert isinstance(s, ConfusionScore)
    assert s.score == 0.0
    assert s.event_count == 0
    assert s.grade == "A"


def test_axiom_violation_with_breach_saturates(tmp_path):
    now = datetime.now(timezone.utc)
    events = [
        {
            "kind": "axiom_violation",
            "delta": 1.0,
            "threshold_breach": True,
            "ts": now.isoformat(),
        }
    ]
    s = compute_confusion_score(events=events, now=now)
    assert s.score == 1.0
    assert s.grade == "F"
    assert s.breach_count == 1
    assert s.by_kind["axiom_violation"] == 1


def test_kr_gap_noise_stays_quiet(tmp_path):
    now = datetime.now(timezone.utc)
    events = [{"kind": "kr_gap", "delta": 0.2, "ts": now.isoformat()} for _ in range(5)]
    s = compute_confusion_score(events=events, now=now)
    # 5 * 0.5 * 0.2 / 3.0 = 0.166...
    assert 0.15 < s.score < 0.2
    assert s.grade == "A"
    assert s.event_count == 5


def test_invalid_kind_skipped(tmp_path):
    now = datetime.now(timezone.utc)
    events = [
        {"kind": "nonsense", "delta": 1.0, "threshold_breach": True, "ts": now.isoformat()},
        {"kind": "evidence_conflict", "delta": 0.5, "ts": now.isoformat()},
    ]
    s = compute_confusion_score(events=events, now=now)
    assert s.event_count == 1
    assert s.by_kind["evidence_conflict"] == 1


def test_window_filter_excludes_old_events(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("SAMUS_CONFUSION_EVENTS_PATH", str(path))
    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=7200)).isoformat()
    fresh = now.isoformat()
    _write_events(
        path,
        [
            {"kind": "axiom_violation", "delta": 1.0, "threshold_breach": True, "ts": old},
            {"kind": "evidence_conflict", "delta": 0.3, "ts": fresh},
        ],
    )
    s = compute_confusion_score(window_seconds=3600, now=now)
    assert s.event_count == 1
    assert s.by_kind["evidence_conflict"] == 1


def test_negative_window_rejected():
    with pytest.raises(ValueError):
        compute_confusion_score(window_seconds=0)


def test_malformed_lines_skipped(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not json\n"
        '{"kind":"kr_gap","delta":0.4,"ts":"' + datetime.now(timezone.utc).isoformat() + '"}\n'
        "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_CONFUSION_EVENTS_PATH", str(path))
    s = compute_confusion_score()
    assert s.event_count == 1


def test_delta_clamped_to_unit_interval():
    now = datetime.now(timezone.utc)
    events = [
        {"kind": "evidence_conflict", "delta": 99.0, "ts": now.isoformat()},
        {"kind": "evidence_conflict", "delta": -5.0, "ts": now.isoformat()},
    ]
    s = compute_confusion_score(events=events, now=now)
    # first clamps to 1.0 (weighted 1.0), second clamps to 0.0
    # weighted = 1.0; score = 1.0/3.0 ≈ 0.333
    assert 0.32 < s.score < 0.35


def test_read_events_returns_empty_when_file_missing(tmp_path):
    p = tmp_path / "absent.jsonl"
    assert read_events(path=p) == []
