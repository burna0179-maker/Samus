"""Tests for backend.cash_engine.stalled_revival.

Focus: pure functions (_parse_created_at, _stalled_opps filtering) + the
sweep tally + the interaction with the injected review_opportunity so the
control-tick's fault-isolation contract holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from backend.cash_engine import stalled_revival as sr


# ---------------------------------------------------------------------------
# _parse_created_at
# ---------------------------------------------------------------------------


def test_parse_created_at_iso_z():
    dt = sr._parse_created_at("2026-05-20T19:14:30Z")
    assert dt is not None and dt.tzinfo is not None


def test_parse_created_at_iso_offset():
    dt = sr._parse_created_at("2026-07-07T00:00:00+00:00")
    assert dt is not None


def test_parse_created_at_none():
    assert sr._parse_created_at(None) is None
    assert sr._parse_created_at("") is None
    assert sr._parse_created_at("garbage") is None


# ---------------------------------------------------------------------------
# _flag_on / _env_int -- shared with sibling modules; smoke-test the branches
# ---------------------------------------------------------------------------


def test_flag_on_default_true(monkeypatch):
    monkeypatch.delenv(sr.ENV_ENABLED, raising=False)
    assert sr._flag_on(sr.ENV_ENABLED) is True


def test_flag_off_variants(monkeypatch):
    for val in ("0", "false", "off", "no"):
        monkeypatch.setenv(sr.ENV_ENABLED, val)
        assert sr._flag_on(sr.ENV_ENABLED) is False


def test_env_int_invalid_falls_back(monkeypatch):
    monkeypatch.setenv(sr.ENV_MAX_PER_SWEEP, "notanint")
    assert sr._env_int(sr.ENV_MAX_PER_SWEEP, 5) == 5


# ---------------------------------------------------------------------------
# run_stalled_revival_sweep -- master switch + injected review paths
# ---------------------------------------------------------------------------


def test_sweep_disabled_master(monkeypatch):
    monkeypatch.setenv(sr.ENV_ENABLED, "0")
    r = sr.run_stalled_revival_sweep()
    assert r == {"enabled": False, "scanned": 0, "revived": 0, "skipped": 0}


class _FakeReview:
    """Stand-in for review_opportunity that a monkeypatch inserts. Returns
    accepted for prospects whose id starts with 'good', blocked otherwise."""

    def __init__(self, pid, source, reason, current):
        self.prospect_id = pid
        self.trigger_source = source

    def __init__(self):  # noqa: F811 — pydantic-style stub
        pass


def _stub_review_module(monkeypatch, accepted_ids):
    """Inject a fake review_opportunity + RevenueTriggerRequest into the
    module import path so _revive_one exercises the accepted / blocked branches
    without touching real cash_engine state."""
    import sys
    import types

    fake_models = types.ModuleType("backend.cash_engine.models")

    class _Req:
        def __init__(self, *, prospect_id, trigger_source, trigger_reason, current_samus_state):
            self.prospect_id = prospect_id

    fake_models.RevenueTriggerRequest = _Req  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.cash_engine.models", fake_models)

    fake_service = types.ModuleType("backend.cash_engine.service")

    class _Result:
        def __init__(self, accepted, status, reason=""):
            self.accepted = accepted
            self.status = status
            self.reason = reason

    def _review(req):
        if req.prospect_id in accepted_ids:
            return _Result(True, "enqueued")
        return _Result(False, "escalated", "no stake sentence")

    fake_service.review_opportunity = _review  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.cash_engine.service", fake_service)


def test_revive_one_accepted(monkeypatch):
    _stub_review_module(monkeypatch, accepted_ids={"pr_ok"})
    r = sr._revive_one({"prospect_id": "pr_ok"})
    assert r["ok"] is True
    assert r["prospect_id"] == "pr_ok"


def test_revive_one_blocked(monkeypatch):
    _stub_review_module(monkeypatch, accepted_ids=set())
    r = sr._revive_one({"prospect_id": "pr_no"})
    assert r["ok"] is False
    assert "stake sentence" in r["reason"]


def test_revive_one_missing_pid(monkeypatch):
    r = sr._revive_one({})
    assert r["ok"] is False
    assert r["reason"] == "missing_prospect_id"


def test_full_sweep_caps_at_max_per_sweep(monkeypatch):
    """A large stalled backlog must not burst-send past max_per_sweep."""
    monkeypatch.delenv(sr.ENV_ENABLED, raising=False)
    monkeypatch.setenv(sr.ENV_MAX_PER_SWEEP, "3")
    now = datetime.now(timezone.utc)

    # 10 opps all older than the age threshold; all 'accepted' by fake review
    fake_rows = [
        {
            "prospect_id": f"pr_{i}",
            "opportunity_id": f"op_{i}",
            "stage": "new",
            "created_at": (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        }
        for i in range(10)
    ]
    monkeypatch.setattr(sr, "_stalled_opps", lambda scan_limit, min_age_hours: fake_rows)
    _stub_review_module(monkeypatch, accepted_ids={f"pr_{i}" for i in range(10)})
    # Heat multiplier full (no throttle)
    import sys
    import types

    heat_mod = types.ModuleType("backend.heat.service")
    heat_mod.send_multiplier_now = lambda: 1.0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.heat.service", heat_mod)
    monkeypatch.setitem(sys.modules, "backend.heat", types.ModuleType("backend.heat"))

    r = sr.run_stalled_revival_sweep()
    assert r["enabled"] is True
    assert r["scanned"] == 10
    assert r["revived"] == 3  # capped by max_per_sweep
    assert r["skipped"] == 7  # the remaining 7 wait for the next tick


def test_age_filter_skips_fresh_opps(monkeypatch):
    """Opps younger than min_age_hours must be excluded so today's fresh
    auto-stakes get a normal cash-engine walk before revival touches them."""
    monkeypatch.setenv(sr.ENV_MIN_AGE_HOURS, "6")
    now = datetime.now(timezone.utc)
    fresh = {
        "stage": "new",
        "created_at": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "prospect_id": "pr_fresh",
    }
    aged = {
        "stage": "new",
        "created_at": (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        "prospect_id": "pr_aged",
    }
    # Bypass DDB by intercepting boto3
    import sys
    import types

    class _FakeTable:
        def scan(self, **_kw):
            return {"Items": [fresh, aged]}

    class _FakeResource:
        def Table(self, name):
            return _FakeTable()

    fake_boto = types.ModuleType("boto3")
    fake_boto.resource = lambda *a, **kw: _FakeResource()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto)

    result = sr._stalled_opps(scan_limit=100, min_age_hours=6)
    ids = [r["prospect_id"] for r in result]
    assert "pr_fresh" not in ids
    assert "pr_aged" in ids
