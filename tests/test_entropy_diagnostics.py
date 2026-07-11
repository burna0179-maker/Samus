"""Diagnostics breadth — four self-healing detectors (HOTL Tranche 5, del. 2).

Verification targets from the plan:
  * a stale heartbeat -> dead-worker detected + operator task within one run
  * a DLQ pending-past-TTL failure -> orphan-task detected + requeue
  * a repeatedly-reprocessed task_id -> stuck-loop detected
  * low disk / oversized ledger dir / high RSS -> resource-exhaustion
Each finding emits a decision.made diagnostic event.
"""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every diagnostics test gets its own DLQ root, coordination dir, events."""
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path / "dlq"))
    monkeypatch.setenv("SAMUS_COORDINATION_DIR", str(tmp_path / "coord"))
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "coord").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _diag_events():
    from backend.common.business_events import DECISION_MADE, read_events

    return [
        e
        for e in read_events(event_types=[DECISION_MADE])
        if (e.get("metadata") or {}).get("decision")
        in (
            "diagnostic",
            "container_restart_request",
        )
    ]


# ---------------------------------------------------------------------------
# stuck-loop
# ---------------------------------------------------------------------------


def test_stuck_loop_detected(monkeypatch):
    from backend.common import dlq
    from backend.entropy import diagnostics

    # Enqueue the same task_id repeatedly with a rising attempt count.
    for attempt in (1, 2, 3, 4):
        dlq.enqueue_failure(
            "seo",
            task_id="t-stuck",
            target="seo",
            payload={},
            error="boom",
            attempt=attempt,
        )
    findings = diagnostics.detect_stuck_loops()
    stuck = [f for f in findings if f.subject == "t-stuck"]
    assert len(stuck) == 1
    assert stuck[0].severity == "critical"
    assert stuck[0].extras["attempts"] == 4

    evs = _diag_events()
    assert any((e["metadata"].get("detector") == "stuck_loop") for e in evs)


def test_stuck_loop_below_threshold_not_flagged(monkeypatch):
    from backend.common import dlq
    from backend.entropy import diagnostics

    dlq.enqueue_failure("seo", task_id="t-ok", target="seo", payload={}, error="x", attempt=1)
    assert diagnostics.detect_stuck_loops() == []


# ---------------------------------------------------------------------------
# dead-worker
# ---------------------------------------------------------------------------


def test_dead_worker_detected_and_operator_task(_isolate, monkeypatch):
    from backend.entropy import diagnostics

    coord = _isolate / "coord"
    stale_ts = time.time() - 300  # 5 min old, well past the 60s default
    (coord / "darwin_heartbeat.json").write_text(
        json.dumps({"agent_id": "darwin", "ts": stale_ts}),
        encoding="utf-8",
    )
    # A fresh heartbeat must NOT be flagged.
    (coord / "samus_heartbeat.json").write_text(
        json.dumps({"agent_id": "samus", "ts": time.time()}),
        encoding="utf-8",
    )

    tasks: list = []
    import backend.crm.service as crm

    monkeypatch.setattr(crm, "create_operator_task", lambda req: tasks.append(req))

    findings = diagnostics.detect_dead_workers()
    dead = [f for f in findings if f.subject == "darwin"]
    assert len(dead) == 1
    assert dead[0].severity == "critical"
    assert "operator_task" in dead[0].remediation

    # Operator task filed within this single run ("within one tick").
    assert len(tasks) == 1
    assert "darwin" in tasks[0].title.lower()

    # A container-restart-request event was emitted alongside the diagnostic.
    evs = _diag_events()
    assert any(e["metadata"].get("decision") == "container_restart_request" for e in evs)
    assert not any(f.subject == "samus" for f in findings)  # fresh one untouched


# ---------------------------------------------------------------------------
# orphan-task
# ---------------------------------------------------------------------------


def test_orphan_task_detected_and_requeued(monkeypatch):
    from backend.common import dlq
    from backend.entropy import diagnostics

    dlq.enqueue_failure(
        "gateway", task_id="t-orphan", target="leadgen", payload={"x": 1}, error="down", attempt=1
    )

    replays: list = []
    import backend.common.replay_worker as rw

    monkeypatch.setattr(rw, "replay_gateway_dlq_sync", lambda limit=25: replays.append(limit) or [])

    # now far in the future so the pending row is "past TTL".
    findings = diagnostics.detect_orphan_tasks(now=time.time() + 10_000)
    orphans = [f for f in findings if f.subject == "t-orphan"]
    assert len(orphans) == 1
    assert orphans[0].severity == "warn"
    assert orphans[0].remediation == "replay_gateway_dlq"
    assert replays == [25]  # requeue actually fired via the replay worker


def test_orphan_task_within_ttl_not_flagged(monkeypatch):
    from backend.common import dlq
    from backend.entropy import diagnostics

    dlq.enqueue_failure(
        "gateway", task_id="t-young", target="leadgen", payload={}, error="x", attempt=1
    )
    # now == enqueue time -> age ~0 < TTL.
    assert diagnostics.detect_orphan_tasks(now=time.time()) == []


def test_orphan_task_skips_already_replayed(monkeypatch):
    from backend.common import dlq
    from backend.entropy import diagnostics

    eid = dlq.enqueue_failure(
        "gateway", task_id="t-done", target="leadgen", payload={}, error="x", attempt=1
    )
    dlq.mark_replayed("gateway", eid, replay_status="replayed")
    findings = diagnostics.detect_orphan_tasks(now=time.time() + 10_000)
    assert not any(f.extras.get("event_id") == eid for f in findings)


# ---------------------------------------------------------------------------
# resource-exhaustion
# ---------------------------------------------------------------------------


def test_resource_exhaustion_low_disk(monkeypatch):
    from backend.entropy import diagnostics

    class _Usage:
        total = 1000
        free = 50  # 5% < 10% default
        used = 950

    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda p: _Usage())
    findings = diagnostics.detect_resource_exhaustion()
    disk = [f for f in findings if f.subject == "disk"]
    assert len(disk) == 1
    assert disk[0].severity == "critical"

    evs = _diag_events()
    assert any(e["metadata"].get("subject") == "disk" for e in evs)


def test_resource_exhaustion_healthy_disk(monkeypatch):
    from backend.entropy import diagnostics

    class _Usage:
        total = 1000
        free = 800  # 80% free
        used = 200

    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda p: _Usage())
    # No data dir, RSS under cap -> no findings.
    monkeypatch.setattr(diagnostics, "_process_rss_mb", lambda: 100.0)
    findings = diagnostics.detect_resource_exhaustion()
    assert not any(f.subject == "disk" for f in findings)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def test_run_diagnostics_aggregates(_isolate, monkeypatch):
    from backend.common import dlq
    from backend.entropy import diagnostics

    dlq.enqueue_failure("seo", task_id="t-x", target="seo", payload={}, error="e", attempt=5)
    monkeypatch.setattr(diagnostics, "_process_rss_mb", lambda: 100.0)

    report = diagnostics.run_diagnostics(remediate=False)
    assert report["healthy"] is False
    assert report["counts"]["critical"] >= 1
    assert any(f["detector"] == "stuck_loop" for f in report["findings"])


def test_run_diagnostics_clean_is_healthy(monkeypatch):
    from backend.entropy import diagnostics

    monkeypatch.setattr(diagnostics, "_process_rss_mb", lambda: 100.0)

    class _Usage:
        total = 1000
        free = 900
        used = 100

    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda p: _Usage())
    report = diagnostics.run_diagnostics()
    assert report["healthy"] is True
    assert report["findings"] == []
