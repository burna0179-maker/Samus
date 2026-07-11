"""Unit tests for the in-container cognitive-observer driver.

Focus is the pure reasoning function ``should_fire_now`` -- the "when" of
Samus grading its own forecasts, isolated from the calibration diagnostic's
I/O. Mirrors ``tests/test_eod_task.py``.
"""
import pytest

import backend.gateway.cognitive_observer_task as co


_ENV_KEYS = (co.ENV_ENABLED, co.ENV_HOUR, co.ENV_LATEST, co.ENV_INTERVAL)


def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    """Point ``storage.root()`` at a tmp dir so the report gate reads nothing."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    # Reset the storage._ROOT back-compat alias so env-var wins in this test.
    from backend.common import storage
    monkeypatch.setattr(storage, "_ROOT", None, raising=False)
    return tmp_path


def _write_report(root, business_date):
    d = root / "cognition"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"calibration_report_{business_date}.json").write_text("{}", encoding="utf-8")


def test_fires_inside_overnight_window_when_not_yet_run(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    fire, reason = co.should_fire_now("2026-07-07", 3)
    assert fire is True, reason
    fire, _ = co.should_fire_now("2026-07-07", 4)
    assert fire is True


def test_skips_before_earliest_hour(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    fire, reason = co.should_fire_now("2026-07-07", 2)
    assert fire is False
    assert "before window" in reason


def test_skips_after_latest_hour(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    fire, reason = co.should_fire_now("2026-07-07", 5)
    assert fire is False
    assert "after window" in reason


def test_respects_custom_window(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    monkeypatch.setenv(co.ENV_HOUR, "1")
    monkeypatch.setenv(co.ENV_LATEST, "3")
    fire, _ = co.should_fire_now("2026-07-07", 1)   # newly inside
    assert fire is True
    fire, _ = co.should_fire_now("2026-07-07", 0)   # still below
    assert fire is False
    fire, _ = co.should_fire_now("2026-07-07", 3)   # at cap (excl)
    assert fire is False


def test_skips_when_master_disabled(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    monkeypatch.setenv(co.ENV_ENABLED, "0")
    fire, reason = co.should_fire_now("2026-07-07", 3)
    assert fire is False
    assert "disabled" in reason


def test_skips_when_report_already_ran_today(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    _write_report(isolated_root, "2026-07-07")   # today's report already exists
    fire, reason = co.should_fire_now("2026-07-07", 3)
    assert fire is False
    assert "already ran" in reason
    # a DIFFERENT day is not suppressed by today's file
    fire, _ = co.should_fire_now("2026-07-08", 3)
    assert fire is True
