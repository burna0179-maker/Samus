"""Tests for the production-failure monitor (backend.observability.production_health).

Covers the exact 2026-07-06 incident shape: an expired Gmail OAuth token while
outbound keeps producing must surface a CRITICAL outbound/inbound asymmetry.
Every check is exercised through its injectable seams so the suite never touches
live settings, the Windows Task Scheduler, or a real mailbox.
"""

from __future__ import annotations

import json

from backend.observability import production_health as ph
from backend.observability.production_health import HealthStatus


# ---------------------------------------------------------------------------
# OAuth token check
# ---------------------------------------------------------------------------


def _write_token(tmp_path, expires_at):
    f = tmp_path / "gmail_oauth_token.json"
    f.write_text(json.dumps({"refresh_token": "x", "access_token": "y", "expires_at": expires_at}))
    return str(f)


def test_oauth_token_expired_is_fail(tmp_path):
    now = 1_000_000.0
    path = _write_token(tmp_path, expires_at=int(now - 4 * 24 * 3600))  # 4 days ago
    check = ph.check_oauth_token(now_ts=now, token_path=path, inbox_configured=True)
    assert check.status == HealthStatus.FAIL
    assert "expired" in check.detail
    assert check.remediation  # actionable fix present


def test_oauth_token_valid_is_ok(tmp_path):
    now = 1_000_000.0
    path = _write_token(tmp_path, expires_at=int(now + 5 * 24 * 3600))
    check = ph.check_oauth_token(now_ts=now, token_path=path, inbox_configured=True)
    assert check.status == HealthStatus.OK


def test_oauth_token_near_expiry_is_ok(tmp_path):
    # A short-lived access token near expiry is healthy - it refreshes each poll,
    # so "expiring soon" is not an alert (that was a false-positive we removed).
    now = 1_000_000.0
    path = _write_token(tmp_path, expires_at=int(now + 1800))  # 30 min out
    check = ph.check_oauth_token(now_ts=now, token_path=path, inbox_configured=True)
    assert check.status == HealthStatus.OK


def test_oauth_token_lapsed_within_grace_is_ok(tmp_path):
    now = 1_000_000.0
    path = _write_token(tmp_path, expires_at=int(now - 1800))  # expired 30 min ago
    check = ph.check_oauth_token(now_ts=now, token_path=path, inbox_configured=True)
    assert check.status == HealthStatus.OK


def test_oauth_token_expired_beyond_grace_is_fail(tmp_path):
    now = 1_000_000.0
    path = _write_token(tmp_path, expires_at=int(now - 3 * 3600))  # 3h ago, beyond 2h grace
    check = ph.check_oauth_token(now_ts=now, token_path=path, inbox_configured=True)
    assert check.status == HealthStatus.FAIL


def test_oauth_token_missing_file_is_fail(tmp_path):
    missing = str(tmp_path / "nope.json")
    check = ph.check_oauth_token(now_ts=1_000_000.0, token_path=missing, inbox_configured=True)
    assert check.status == HealthStatus.FAIL
    assert "missing" in check.detail


def test_oauth_token_inbox_not_configured_is_info(tmp_path):
    check = ph.check_oauth_token(now_ts=1_000_000.0, token_path="", inbox_configured=False)
    assert check.status == HealthStatus.INFO


# ---------------------------------------------------------------------------
# Scheduled-task check (parse layer, platform-independent via monkeypatch)
# ---------------------------------------------------------------------------


def test_scheduled_task_nonzero_result_is_fail(monkeypatch):
    monkeypatch.setattr(ph, "_on_windows", lambda: True)
    monkeypatch.setattr(
        ph,
        "_query_scheduled_task",
        lambda name: {"last_result": "1", "last_run": "7/6/2026 8:40 AM", "status": "Ready"},
    )
    checks = ph.check_scheduled_tasks(task_names=("Samus Inbox Poll",))
    assert len(checks) == 1
    assert checks[0].status == HealthStatus.FAIL
    assert "exit 1" in checks[0].detail


def test_scheduled_task_zero_result_is_ok(monkeypatch):
    monkeypatch.setattr(ph, "_on_windows", lambda: True)
    monkeypatch.setattr(
        ph,
        "_query_scheduled_task",
        lambda name: {"last_result": "0", "last_run": "7/6/2026 8:40 AM", "status": "Ready"},
    )
    checks = ph.check_scheduled_tasks(task_names=("Samus Inbox Poll",))
    assert checks[0].status == HealthStatus.OK


def test_scheduled_task_non_windows_is_unknown(monkeypatch):
    monkeypatch.setattr(ph, "_on_windows", lambda: False)
    checks = ph.check_scheduled_tasks(task_names=("Samus Inbox Poll",))
    assert len(checks) == 1
    assert checks[0].status == HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Outbound activity + asymmetry
# ---------------------------------------------------------------------------


class _FakeObs:
    def __init__(self, ts):
        self.last_activity_ts = ts


def test_outbound_recent_activity(monkeypatch):
    now = 1_000_000.0
    check, produced = ph.check_outbound_activity(now_ts=now, observer=lambda: _FakeObs(now - 3600))
    assert produced is True
    assert check.status == HealthStatus.INFO


def test_outbound_stale_activity_not_recent():
    now = 1_000_000.0
    check, produced = ph.check_outbound_activity(
        now_ts=now, observer=lambda: _FakeObs(now - 5 * 24 * 3600)
    )
    assert produced is False


def test_asymmetry_critical_when_outbound_live_inbound_down():
    check = ph.evaluate_asymmetry(inbound_down=True, outbound_recent=True)
    assert check is not None
    assert check.status == HealthStatus.CRITICAL
    assert "CAN-SPAM" in check.detail


def test_asymmetry_none_when_inbound_healthy():
    assert ph.evaluate_asymmetry(inbound_down=False, outbound_recent=True) is None


def test_asymmetry_none_when_no_outbound():
    assert ph.evaluate_asymmetry(inbound_down=True, outbound_recent=False) is None


# ---------------------------------------------------------------------------
# Orchestrator - the incident reproduction
# ---------------------------------------------------------------------------


def test_report_reproduces_incident(monkeypatch):
    """Expired token + live outbound => CRITICAL asymmetry surfaces."""
    now = 1_000_000.0
    monkeypatch.setattr(
        ph,
        "check_oauth_token",
        lambda **kw: ph.HealthCheck("oauth_token", HealthStatus.FAIL, "expired 94h ago"),
    )
    monkeypatch.setattr(ph, "check_scheduled_tasks", lambda **kw: [])
    monkeypatch.setattr(
        ph,
        "check_inbound_freshness",
        lambda **kw: ph.HealthCheck("inbound_freshness", HealthStatus.INFO, "stale"),
    )
    monkeypatch.setattr(
        ph,
        "check_outbound_activity",
        lambda **kw: (ph.HealthCheck("outbound_activity", HealthStatus.INFO, "1h ago"), True),
    )
    report = ph.check_production_health(now_ts=now)
    assert report.enabled
    assert report.worst_status == HealthStatus.CRITICAL
    assert report.has_failures()
    names = {c.name for c in report.alerts()}
    assert "outbound_inbound_asymmetry" in names
    assert "oauth_token" in names


def test_report_clean_when_healthy(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(
        ph,
        "check_oauth_token",
        lambda **kw: ph.HealthCheck("oauth_token", HealthStatus.OK, "valid 120h"),
    )
    monkeypatch.setattr(
        ph,
        "check_scheduled_tasks",
        lambda **kw: [ph.HealthCheck("task:Samus Inbox Poll", HealthStatus.OK, "ok")],
    )
    monkeypatch.setattr(
        ph,
        "check_inbound_freshness",
        lambda **kw: ph.HealthCheck("inbound_freshness", HealthStatus.INFO, "0.2h ago"),
    )
    monkeypatch.setattr(
        ph,
        "check_outbound_activity",
        lambda **kw: (ph.HealthCheck("outbound_activity", HealthStatus.INFO, "1h ago"), True),
    )
    report = ph.check_production_health(now_ts=now)
    assert not report.has_alerts()
    # INFO checks (freshness / last-activity) legitimately outrank OK; the point
    # is simply that nothing reaches alert level.
    assert report.worst_status not in (HealthStatus.WARN, HealthStatus.FAIL, HealthStatus.CRITICAL)


def test_report_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("SAMUS_PRODUCTION_HEALTH_CHECK_ENABLED", "0")
    report = ph.check_production_health(now_ts=1_000_000.0)
    assert report.enabled is False
    assert report.checks == []
