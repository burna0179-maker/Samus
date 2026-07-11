"""Tests for the fast-cadence email alerting (production_health_notify).

Verifies the state-change throttle: a new failure alerts once, a persistent
failure stays silent, and a recovery sends exactly one note. All I/O and the
email sender are injected so nothing touches a real mailbox or storage root.
"""
from __future__ import annotations

from backend.observability import production_health_notify as notify
from backend.observability.production_health import (
    HealthCheck,
    HealthStatus,
    ProductionHealthReport,
)


def _report(*checks, enabled=True):
    return ProductionHealthReport(checks=list(checks), generated_ts=1_000_000.0, enabled=enabled)


def _fail_report():
    return _report(
        HealthCheck("oauth_token", HealthStatus.FAIL, "expired"),
        HealthCheck("outbound_activity", HealthStatus.INFO, "1h ago"),
    )


def _clean_report():
    return _report(HealthCheck("oauth_token", HealthStatus.OK, "valid"))


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"message_id": "test"}


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_empty_when_no_alerts():
    assert notify._fingerprint(_clean_report()) == ""


def test_fingerprint_changes_with_alert_set():
    fp1 = notify._fingerprint(_fail_report())
    fp2 = notify._fingerprint(_report(HealthCheck("task:x", HealthStatus.FAIL, "boom")))
    assert fp1 and fp2 and fp1 != fp2


# ---------------------------------------------------------------------------
# Dispatch: state-change throttle
# ---------------------------------------------------------------------------

def test_new_failure_alerts(tmp_path):
    rec = _Recorder()
    sp = tmp_path / "state.json"
    result = notify.dispatch(
        report=_fail_report(), sender=rec, recipient="op@example.com",
        state_path=sp, now_ts=1_000_000.0,
    )
    assert result["action"] == "alerted"
    assert len(rec.calls) == 1
    assert "PRODUCTION ALERT" in rec.calls[0]["subject"]
    assert rec.calls[0]["to"] == "op@example.com"
    assert sp.exists()  # state persisted


def test_persistent_failure_is_silent(tmp_path):
    rec = _Recorder()
    sp = tmp_path / "state.json"
    # First alert persists the fingerprint.
    notify.dispatch(report=_fail_report(), sender=rec, recipient="op@example.com",
                    state_path=sp, now_ts=1_000_000.0)
    # Same failure again => no second email.
    result = notify.dispatch(report=_fail_report(), sender=rec, recipient="op@example.com",
                             state_path=sp, now_ts=1_000_900.0)
    assert result["action"] == "unchanged"
    assert len(rec.calls) == 1  # still just the one


def test_recovery_sends_one_note(tmp_path):
    rec = _Recorder()
    sp = tmp_path / "state.json"
    notify.dispatch(report=_fail_report(), sender=rec, recipient="op@example.com",
                    state_path=sp, now_ts=1_000_000.0)
    result = notify.dispatch(report=_clean_report(), sender=rec, recipient="op@example.com",
                             state_path=sp, now_ts=1_001_000.0)
    assert result["action"] == "recovered"
    assert len(rec.calls) == 2
    assert "RECOVERED" in rec.calls[1]["subject"]


def test_clean_to_clean_is_silent(tmp_path):
    rec = _Recorder()
    sp = tmp_path / "state.json"
    result = notify.dispatch(report=_clean_report(), sender=rec, recipient="op@example.com",
                             state_path=sp, now_ts=1_000_000.0)
    assert result["action"] == "unchanged"
    assert rec.calls == []


def test_no_recipient_records_state_and_skips(tmp_path):
    rec = _Recorder()
    sp = tmp_path / "state.json"
    result = notify.dispatch(report=_fail_report(), sender=rec, recipient="",
                             state_path=sp, now_ts=1_000_000.0)
    assert result["action"] == "no_recipient"
    assert rec.calls == []
    assert sp.exists()  # recorded so it does not loop forever


def test_send_failure_does_not_persist_state(tmp_path):
    def boom(**kwargs):
        raise RuntimeError("smtp down")

    sp = tmp_path / "state.json"
    result = notify.dispatch(report=_fail_report(), sender=boom, recipient="op@example.com",
                             state_path=sp, now_ts=1_000_000.0)
    assert result["action"] == "send_failed"
    assert not sp.exists()  # not persisted => next cycle retries


def test_disabled_report_no_action(tmp_path):
    rec = _Recorder()
    result = notify.dispatch(
        report=_report(HealthCheck("x", HealthStatus.FAIL, "y"), enabled=False),
        sender=rec, recipient="op@example.com", state_path=tmp_path / "s.json",
    )
    assert result["action"] == "disabled"
    assert rec.calls == []
