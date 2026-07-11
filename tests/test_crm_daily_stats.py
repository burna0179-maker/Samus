"""CRM ``service.daily_stats`` + ``GET /crm/metrics/daily-stats`` — call
state roll-up powering the gateway's Samus HUD endpoint.

We monkeypatch ``backend.crm.persistence.safe_scan`` directly rather than
extending the shared ``_FakeTable`` shim, because the new helper uses a
``begins_with(...)`` filter the shim doesn't model (the shim only handles
single-attr equality). This keeps the existing CRM service tests
untouched while exercising the new branch.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any


def _stub_call_states(monkeypatch, items: list[dict[str, Any]]) -> None:
    """Make persistence.safe_scan return ``items`` for any filter.

    daily_stats calls _call_state_table() (which would normally hit boto3
    to build a DynamoDB Table handle) and passes it to safe_scan with a
    begins_with(updated_at) filter. The stubs return a sentinel table
    handle + the canned rows regardless of the filter expression, so the
    test bypasses boto3 entirely.
    """
    import backend.crm.persistence as p

    monkeypatch.setattr(p, "_call_state_table", lambda: object())

    def _fake_safe_scan(*args, **kwargs):
        return list(items), False, None

    monkeypatch.setattr(p, "safe_scan", _fake_safe_scan)


def test_daily_stats_buckets_by_outcome(monkeypatch):
    today = _dt.datetime.utcnow().date().isoformat()
    _stub_call_states(monkeypatch, [
        {"prospect_id": "p1", "state": "completed",
         "last_outcome": "booked", "updated_at": f"{today}T09:00:00Z"},
        {"prospect_id": "p2", "state": "completed",
         "last_outcome": "follow_up", "updated_at": f"{today}T10:00:00Z"},
        {"prospect_id": "p3", "state": "completed",
         "last_outcome": "follow_up", "updated_at": f"{today}T11:00:00Z"},
        {"prospect_id": "p4", "state": "no_answer",
         "last_outcome": "no_answer", "updated_at": f"{today}T12:00:00Z"},
        {"prospect_id": "p5", "state": "completed",
         "last_outcome": "noted", "updated_at": f"{today}T13:00:00Z"},
    ])
    from backend.crm.service import daily_stats
    out = daily_stats(today)
    assert out["date"] == today
    assert out["calls_today"] == 5
    assert out["booked_today"] == 1
    assert out["followups_today"] == 2
    assert out["scan_truncated"] is False
    assert out["ddb_error"] is None


def test_daily_stats_zero_when_no_rows(monkeypatch):
    today = _dt.datetime.utcnow().date().isoformat()
    _stub_call_states(monkeypatch, [])
    from backend.crm.service import daily_stats
    out = daily_stats(today)
    assert out["calls_today"] == 0
    assert out["booked_today"] == 0
    assert out["followups_today"] == 0


def test_daily_stats_outcome_case_insensitive(monkeypatch):
    """last_outcome stored upper / mixed case still buckets correctly —
    operator-typed notes can carry surprising casing."""
    today = _dt.datetime.utcnow().date().isoformat()
    _stub_call_states(monkeypatch, [
        {"prospect_id": "p1", "state": "completed",
         "last_outcome": "Booked", "updated_at": f"{today}T09:00:00Z"},
        {"prospect_id": "p2", "state": "completed",
         "last_outcome": " FOLLOW_UP ", "updated_at": f"{today}T10:00:00Z"},
    ])
    from backend.crm.service import daily_stats
    out = daily_stats(today)
    assert out["booked_today"] == 1
    assert out["followups_today"] == 1


def test_daily_stats_skips_malformed_rows(monkeypatch):
    """A malformed CallState row (missing PK, bad type) is skipped — one
    bad row must not zero out the whole counter."""
    today = _dt.datetime.utcnow().date().isoformat()
    _stub_call_states(monkeypatch, [
        {"prospect_id": "p1", "state": "completed",
         "last_outcome": "booked", "updated_at": f"{today}T09:00:00Z"},
        # malformed — state value is not in the Literal
        {"prospect_id": "p2", "state": "GARBAGE", "last_outcome": "booked",
         "updated_at": f"{today}T10:00:00Z"},
    ])
    from backend.crm.service import daily_stats
    out = daily_stats(today)
    assert out["calls_today"] == 1
    assert out["booked_today"] == 1


def _capture_scan_call(monkeypatch, items):
    """Like _stub_call_states but records the filter kwargs safe_scan saw, so a
    test can assert WHICH selection mode (begins_with vs BETWEEN) ran."""
    import backend.crm.persistence as p
    captured: dict[str, Any] = {}
    monkeypatch.setattr(p, "_call_state_table", lambda: object())

    def _fake_safe_scan(*args, **kwargs):
        captured.update(kwargs)
        return list(items), False, None

    monkeypatch.setattr(p, "safe_scan", _fake_safe_scan)
    return captured


def test_daily_stats_default_uses_begins_with(monkeypatch):
    """No start/end -> legacy UTC-day prefix scan (unchanged behavior)."""
    cap = _capture_scan_call(monkeypatch, [])
    from backend.crm.service import daily_stats
    daily_stats("2026-07-03")
    assert cap["filter_expression"] == "begins_with(#u, :today)"
    assert cap["expression_attribute_values"] == {":today": "2026-07-03"}


def test_daily_stats_range_uses_between(monkeypatch):
    """start+end -> BETWEEN range scan (the Pacific-business-day path). The
    label ``date`` still echoes ``today``."""
    cap = _capture_scan_call(monkeypatch, [
        {"prospect_id": "p1", "state": "completed",
         "last_outcome": "booked", "updated_at": "2026-07-03T23:30:00Z"},
    ])
    from backend.crm.service import daily_stats
    out = daily_stats(
        "2026-07-03",
        start="2026-07-03T07:00:00Z", end="2026-07-04T07:00:00Z",
    )
    assert cap["filter_expression"] == "#u BETWEEN :start AND :end"
    assert cap["expression_attribute_values"] == {
        ":start": "2026-07-03T07:00:00Z", ":end": "2026-07-04T07:00:00Z",
    }
    assert out["date"] == "2026-07-03"
    assert out["calls_today"] == 1
    assert out["booked_today"] == 1


def test_business_day_utc_bounds_dst_aware():
    """Pacific business day maps to the correct UTC range in both DST regimes:
    PDT (UTC-7) in summer, PST (UTC-8) in winter."""
    from backend.common.dates import business_day_utc_bounds
    assert business_day_utc_bounds("2026-07-03") == (
        "2026-07-03T07:00:00Z", "2026-07-04T07:00:00Z")   # PDT
    assert business_day_utc_bounds("2026-01-15") == (
        "2026-01-15T08:00:00Z", "2026-01-16T08:00:00Z")   # PST


def test_daily_stats_surfaces_ddb_error(monkeypatch):
    """A backend failure returns zeroes with the error string passed
    through — operator dashboards key on ``ddb_error`` to render a
    degraded tile instead of a misleading zero."""
    import backend.crm.persistence as p
    monkeypatch.setattr(p, "_call_state_table", lambda: object())
    monkeypatch.setattr(
        p, "safe_scan",
        lambda *a, **kw: ([], False, "ddb_scan_failed: simulated"),
    )
    from backend.crm.service import daily_stats
    today = _dt.datetime.utcnow().date().isoformat()
    out = daily_stats(today)
    assert out["calls_today"] == 0
    assert out["ddb_error"] == "ddb_scan_failed: simulated"
