"""Doc §3.6 — enqueue_failure / read_pending / mark_replayed / read_archive."""
from __future__ import annotations

import json


def test_enqueue_failure_appends_to_per_service_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path))
    from backend.common import dlq

    event_id = dlq.enqueue_failure(
        "leadgen",
        task_id="t1",
        target="leadgen",
        payload={"a": 1},
        error="connection refused",
        attempt=1,
    )
    assert isinstance(event_id, str) and len(event_id) >= 8

    failures_path = tmp_path / "leadgen_failures.jsonl"
    assert failures_path.exists()
    lines = failures_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_id"] == event_id
    assert record["service"] == "leadgen"
    assert record["task_id"] == "t1"
    assert record["target"] == "leadgen"
    assert record["payload"] == {"a": 1}
    assert record["error"] == "connection refused"
    assert record["status"] == "pending_retry"
    assert record["attempt"] == 1
    assert "ts" in record


def test_read_pending_returns_recent_records(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path))
    from backend.common import dlq

    for i in range(5):
        dlq.enqueue_failure(
            "scaffold",
            task_id=f"t{i}",
            target="scaffold",
            payload={},
            error="boom",
        )
    pending = dlq.read_pending("scaffold", limit=3)
    assert len(pending) == 3
    assert [p["task_id"] for p in pending] == ["t2", "t3", "t4"]


def test_read_pending_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path))
    from backend.common import dlq
    assert dlq.read_pending("nonexistent") == []


def test_mark_replayed_writes_to_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path))
    from backend.common import dlq

    dlq.mark_replayed("gateway", "evt-123", replay_status="replayed")
    archive = tmp_path / "replayed_archive.jsonl"
    assert archive.exists()
    record = json.loads(archive.read_text(encoding="utf-8").strip())
    assert record["event_id"] == "evt-123"
    assert record["service"] == "gateway"
    assert record["status"] == "replayed"


def test_read_archive_returns_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path))
    from backend.common import dlq

    for i in range(3):
        dlq.mark_replayed("gateway", f"evt-{i}", replay_status="replayed")
    out = dlq.read_archive(limit=2)
    assert [r["event_id"] for r in out] == ["evt-1", "evt-2"]


def test_read_archive_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path))
    from backend.common import dlq
    assert dlq.read_archive() == []
