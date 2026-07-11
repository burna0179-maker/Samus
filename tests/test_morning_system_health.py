"""Morning brief SYSTEM HEALTH section (HOTL T5) — diagnostics + weak workcells."""
from __future__ import annotations

import datetime as _dt
import json
import time

import pytest

from backend import morning


TODAY = _dt.date.today()


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path / "dlq"))
    monkeypatch.setenv("SAMUS_COORDINATION_DIR", str(tmp_path / "coord"))
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_REPUTATION_PATH", str(tmp_path / "rep.json"))
    monkeypatch.setenv("SAMUS_ROI_ROLLUP_PATH", str(tmp_path / "roi.json"))
    monkeypatch.setenv("DDB_PORTFOLIO_SNAPSHOTS_TABLE", "")
    (tmp_path / "coord").mkdir(parents=True, exist_ok=True)


def test_system_health_omitted_when_clean(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    # No diagnostics, no weak workcells -> section omitted.
    import backend.entropy.diagnostics as diag
    monkeypatch.setattr(diag, "_process_rss_mb", lambda: 100.0)

    class _Usage:
        total, free, used = 1000, 900, 100

    monkeypatch.setattr(diag.shutil, "disk_usage", lambda p: _Usage())
    assert morning._render_system_health(TODAY) == []


def test_system_health_shows_diagnostics(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    # A stale heartbeat -> dead-worker finding in the section.
    coord = tmp_path / "coord"
    (coord / "darwin_heartbeat.json").write_text(
        json.dumps({"agent_id": "darwin", "ts": time.time() - 500}),
        encoding="utf-8",
    )
    # Don't let the (detection-only) render file an operator task.
    import backend.crm.service as crm
    monkeypatch.setattr(crm, "create_operator_task", lambda req: None)

    lines = morning._render_system_health(TODAY)
    text = "\n".join(lines)
    assert "SYSTEM HEALTH" in text
    assert "dead_worker" in text
