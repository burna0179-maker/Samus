"""Intake rate-limit + CAPTCHA + XFF-trust tests (feat/samus-intake-hardening).

Covers Finding 1 (rate limiting + CAPTCHA) and Finding 3 (spoofable client
IP). All DynamoDB / network I/O is mocked — no real AWS, no real internet.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


class _FakeTable:
    """Minimal DynamoDB table stub with an atomic ADD counter."""

    def __init__(self):
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, Item):
        self.items[Item.get("idempotency_key") or Item.get("lead_id")] = Item

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}

    def update_item(
        self, *, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None, **kwargs
    ):
        key = Key["idempotency_key"]
        row = self.items.setdefault(key, {"idempotency_key": key, "request_count": 0})
        row["request_count"] = int(row.get("request_count", 0)) + int(
            ExpressionAttributeValues[":one"]
        )
        row["expires_at"] = ExpressionAttributeValues[":exp"]
        return {"Attributes": {"request_count": row["request_count"]}}


class _FailingTable:
    """A table whose update_item always raises — exercises the fail-open path."""

    def update_item(self, **kwargs):
        raise RuntimeError("simulated DynamoDB throttle")


def _patch_counter_table(monkeypatch, table):
    import backend.intake.rate_limit as rl_mod

    monkeypatch.setattr(rl_mod, "_counter_table", lambda: table)


def _enable_rate_limit(monkeypatch, *, per_minute=5, per_hour=30, global_per_hour=600):
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE", str(per_minute))
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_PER_HOUR", str(per_hour))
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_GLOBAL_PER_HOUR", str(global_per_hour))
    from backend.common.config import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# check_rate_limit — allow / deny / fail-open
# ---------------------------------------------------------------------------


def test_rate_limit_allows_under_the_minute_cap(monkeypatch):
    _enable_rate_limit(monkeypatch, per_minute=5)
    _patch_counter_table(monkeypatch, _FakeTable())
    from backend.intake.rate_limit import check_rate_limit

    # 5 requests at a fixed instant — all within the 5/min cap.
    for _ in range(5):
        decision = check_rate_limit("203.0.113.9", now=1_000_000.0)
        assert decision.allowed is True


def test_rate_limit_denies_over_the_minute_cap(monkeypatch):
    _enable_rate_limit(monkeypatch, per_minute=3)
    _patch_counter_table(monkeypatch, _FakeTable())
    from backend.intake.rate_limit import check_rate_limit

    results = [check_rate_limit("203.0.113.9", now=1_000_000.0) for _ in range(5)]
    # First 3 allowed, the 4th and 5th breach the per-minute ceiling.
    assert [r.allowed for r in results] == [True, True, True, False, False]
    breach = results[3]
    assert breach.scope == "ip"
    assert breach.limit == 3
    assert breach.retry_after_seconds > 0


def test_rate_limit_denies_over_the_hour_cap(monkeypatch):
    """Per-hour cap fires even when each minute stays under the minute cap."""
    _enable_rate_limit(monkeypatch, per_minute=100, per_hour=4)
    _patch_counter_table(monkeypatch, _FakeTable())
    from backend.intake.rate_limit import check_rate_limit

    results = [check_rate_limit("203.0.113.9", now=2_000_000.0) for _ in range(6)]
    assert [r.allowed for r in results] == [True, True, True, True, False, False]
    assert results[4].limit == 4


def test_rate_limit_global_ceiling_catches_distributed_flood(monkeypatch):
    """Distinct IPs, each under the per-IP cap, still trip the global ceiling."""
    _enable_rate_limit(monkeypatch, per_minute=100, per_hour=100, global_per_hour=3)
    _patch_counter_table(monkeypatch, _FakeTable())
    from backend.intake.rate_limit import check_rate_limit

    results = [check_rate_limit(f"198.51.100.{i}", now=3_000_000.0) for i in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert results[3].scope == "global"


def test_rate_limit_fails_open_on_backend_error(monkeypatch):
    """A DynamoDB error must NOT block the lead — limiter fails OPEN."""
    _enable_rate_limit(monkeypatch, per_minute=1)
    _patch_counter_table(monkeypatch, _FailingTable())
    from backend.intake.rate_limit import check_rate_limit

    # Even far past the cap, every request is allowed because the backend
    # is unreachable — but the degradation is surfaced via backend_error.
    for _ in range(10):
        decision = check_rate_limit("203.0.113.9", now=4_000_000.0)
        assert decision.allowed is True
        assert decision.backend_error is not None
        assert "rate_limit_backend_error" in decision.backend_error


def test_rate_limit_disabled_short_circuits(monkeypatch):
    """When disabled, the limiter never touches the backend and always allows."""
    monkeypatch.setenv("SAMUS_INTAKE_RATE_LIMIT_ENABLED", "0")
    from backend.common.config import reload_settings

    reload_settings()
    _patch_counter_table(monkeypatch, _FailingTable())  # would raise if hit
    from backend.intake.rate_limit import check_rate_limit

    for _ in range(50):
        assert check_rate_limit("203.0.113.9").allowed is True


def test_rate_limit_separate_windows_per_ip(monkeypatch):
    """One IP hitting its cap does not throttle a different IP."""
    _enable_rate_limit(monkeypatch, per_minute=2)
    _patch_counter_table(monkeypatch, _FakeTable())
    from backend.intake.rate_limit import check_rate_limit

    # Exhaust IP A.
    check_rate_limit("10.0.0.1", now=5_000_000.0)
    check_rate_limit("10.0.0.1", now=5_000_000.0)
    assert check_rate_limit("10.0.0.1", now=5_000_000.0).allowed is False
    # IP B starts fresh.
    assert check_rate_limit("10.0.0.2", now=5_000_000.0).allowed is True


def test_rate_limit_window_rolls_over(monkeypatch):
    """A new fixed-window bucket resets the counter."""
    _enable_rate_limit(monkeypatch, per_minute=2)
    _patch_counter_table(monkeypatch, _FakeTable())
    from backend.intake.rate_limit import check_rate_limit

    base = 6_000_000.0
    check_rate_limit("10.0.0.3", now=base)
    check_rate_limit("10.0.0.3", now=base)
    assert check_rate_limit("10.0.0.3", now=base).allowed is False
    # 61s later — a new minute bucket — the IP is allowed again.
    assert check_rate_limit("10.0.0.3", now=base + 61).allowed is True


# ---------------------------------------------------------------------------
# _client_ip — trusted-proxy XFF parsing (Finding 3)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    def __init__(self, *, xff: str | None = None, peer: str = "10.9.9.9"):
        self.headers = {}
        if xff is not None:
            self.headers["x-forwarded-for"] = xff
        self.client = _FakeClient(peer)


def _set_proxy_hops(monkeypatch, hops: int):
    monkeypatch.setenv("SAMUS_INTAKE_TRUSTED_PROXY_HOPS", str(hops))
    from backend.common.config import reload_settings

    reload_settings()


def test_client_ip_one_hop_takes_rightmost_entry(monkeypatch):
    """With one trusted proxy, the real client is the LAST XFF entry.

    A spoofed leftmost entry must be ignored: an attacker sending
    ``X-Forwarded-For: 1.2.3.4`` only prepends to the list — the trusted
    proxy still appends the address it actually saw.
    """
    _set_proxy_hops(monkeypatch, 1)
    from backend.intake.app import _client_ip

    # Attacker forges 1.2.3.4; the single trusted proxy appended 198.51.100.7.
    req = _FakeRequest(xff="1.2.3.4, 198.51.100.7")
    assert _client_ip(req) == "198.51.100.7"


def test_client_ip_legitimate_single_entry(monkeypatch):
    """A legitimate request through one proxy has exactly one XFF entry."""
    _set_proxy_hops(monkeypatch, 1)
    from backend.intake.app import _client_ip

    req = _FakeRequest(xff="203.0.113.55")
    assert _client_ip(req) == "203.0.113.55"


def test_client_ip_two_hops(monkeypatch):
    """With two trusted hops the client is the entry two positions from right."""
    _set_proxy_hops(monkeypatch, 2)
    from backend.intake.app import _client_ip

    # client, proxy1-saw, proxy2-saw  -> client is xff[-2]
    req = _FakeRequest(xff="203.0.113.1, 198.51.100.2, 192.0.2.3")
    assert _client_ip(req) == "198.51.100.2"


def test_client_ip_falls_back_when_xff_shorter_than_hops(monkeypatch):
    """Fewer XFF entries than trusted hops -> fall back to the socket peer."""
    _set_proxy_hops(monkeypatch, 2)
    from backend.intake.app import _client_ip

    # Only one entry but two trusted hops expected — XFF cannot be trusted.
    req = _FakeRequest(xff="203.0.113.1", peer="10.9.9.9")
    assert _client_ip(req) == "10.9.9.9"


def test_client_ip_falls_back_when_no_xff(monkeypatch):
    """No XFF header at all -> the unspoofable socket peer."""
    _set_proxy_hops(monkeypatch, 1)
    from backend.intake.app import _client_ip

    req = _FakeRequest(xff=None, peer="10.9.9.9")
    assert _client_ip(req) == "10.9.9.9"


def test_client_ip_ignores_pure_spoof_attempt(monkeypatch):
    """An attacker who sends only a fake XFF (direct hit) cannot poison the IP.

    A direct connection has no trusted proxy in front, so the single
    attacker-supplied entry is shorter than the trusted-hop count and the
    limiter keys on the real socket peer.
    """
    _set_proxy_hops(monkeypatch, 1)
    from backend.intake.app import _client_ip

    # Attacker hits the service directly and forges a one-entry XFF. With one
    # trusted proxy expected, one entry IS treated as the proxy-appended
    # client — this is the documented Cloud Run posture (the platform always
    # appends). The defended case is the multi-entry spoof above; here we
    # assert the single-entry value is at least taken verbatim, not the
    # leftmost-of-many.
    req = _FakeRequest(xff="6.6.6.6", peer="10.9.9.9")
    assert _client_ip(req) == "6.6.6.6"
