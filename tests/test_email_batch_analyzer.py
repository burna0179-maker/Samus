"""Tests for backend.outreach.email_batch_analyzer — the deliverability/
reputation audit. HTTP mocked with httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx

from backend.outreach import email_batch_analyzer as eba


def _stats(**metrics):
    base = {k: 0 for k in eba._METRIC_KEYS}
    base.update(metrics)
    return [{"date": "2026-07-01", "stats": [{"metrics": base}]}]


def _client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v3/stats" in request.url.path
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_stats_returns_list():
    with _client(_stats(requests=7)) as c:
        got = eba.fetch_stats("K", start_date="2026-07-01", http_client=c)
    assert isinstance(got, list) and got[0]["date"] == "2026-07-01"


def test_fetch_stats_failsoft():
    def handler(request):
        return httpx.Response(500, json={"errors": ["boom"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert eba.fetch_stats("K", start_date="2026-07-01", http_client=c) == []


def test_aggregate_rates():
    daily = _stats(
        requests=100,
        delivered=95,
        bounces=3,
        blocks=1,
        spam_reports=0,
        unique_opens=38,
        unique_clicks=5,
        unsubscribes=1,
    )
    m = eba.aggregate(daily, window_start="a", window_end="b")
    assert m.requests == 100 and m.delivered == 95
    assert m.delivery_rate == 0.95
    assert m.bounce_rate == 0.03
    assert m.block_rate == 0.01
    assert m.spam_rate == 0.0
    assert m.open_rate == round(38 / 95, 4)


def test_aggregate_zero_requests_no_div_by_zero():
    m = eba.aggregate(_stats(), window_start="a", window_end="b")
    assert m.requests == 0 and m.delivery_rate == 0.0 and m.bounce_rate == 0.0


def test_reputation_alert_spam_is_critical():
    m = eba.aggregate(
        _stats(requests=100, delivered=100, spam_reports=1), window_start="a", window_end="b"
    )
    alerts = eba.reputation_alerts(m)
    assert any(a.severity == "critical" and a.metric == "spam_rate" for a in alerts)


def test_reputation_alert_high_bounce_is_warning():
    m = eba.aggregate(
        _stats(requests=100, delivered=90, bounces=8), window_start="a", window_end="b"
    )
    alerts = eba.reputation_alerts(m)
    assert any(a.severity == "warning" and a.metric == "bounce_rate" for a in alerts)


def test_reputation_healthy_no_alerts():
    m = eba.aggregate(
        _stats(requests=100, delivered=99, bounces=1), window_start="a", window_end="b"
    )
    assert eba.reputation_alerts(m) == []


def test_autonomous_audit_persists_reports_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(eba.storage, "root", lambda: tmp_path)
    payload = _stats(requests=7, delivered=7)
    m1 = eba.autonomous_audit(api_key="K", http_client=_client(payload))
    assert m1 is not None and m1.requests == 7
    assert (tmp_path / "outreach" / "email_audits" / "latest.txt").exists()
    store = tmp_path / "outreach" / "email_stats_analyses.jsonl"
    assert len([l for l in store.read_text().splitlines() if l.strip()]) == 1
    # same volume -> dedup
    eba.autonomous_audit(api_key="K", http_client=_client(payload))
    assert len([l for l in store.read_text().splitlines() if l.strip()]) == 1


def test_autonomous_audit_writes_alert_on_spam(tmp_path, monkeypatch):
    monkeypatch.setattr(eba.storage, "root", lambda: tmp_path)
    payload = _stats(requests=100, delivered=100, spam_reports=2)
    m = eba.autonomous_audit(api_key="K", http_client=_client(payload))
    assert m.spam_reports == 2
    alerts = list((tmp_path / "outreach" / "email_audit_alerts").glob("alert_*.json"))
    assert len(alerts) == 1
    data = json.loads(alerts[0].read_text())
    assert any(a["metric"] == "spam_rate" for a in data["alerts"])


def test_autonomous_audit_no_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(eba.storage, "root", lambda: tmp_path)
    assert eba.autonomous_audit(api_key="") is None


def test_autonomous_audit_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(eba.storage, "root", lambda: tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(eba, "aggregate", _boom)
    assert eba.autonomous_audit(api_key="K", http_client=_client(_stats())) is None
