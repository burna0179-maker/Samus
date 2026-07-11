"""Unit tests for the in-container EOD driver.

Focus is the pure reasoning function should_fire_now -- the "when" of Samus
closing out its own day, isolated from run_end_of_day_review's I/O. Mirrors
tests/test_morning_ritual_task.py.
"""

import backend.gateway.eod_task as eod


_ENV_KEYS = (eod.ENV_ENABLED, eod.ENV_HOUR, eod.ENV_LATEST, eod.ENV_INTERVAL)


def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


import pytest


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    """Point storage.root() at a tmp dir so the eod_review gate reads nothing."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


def _write_review(root, business_date):
    d = root / "cognition"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"eod_review_{business_date}.md").write_text("x", encoding="utf-8")


def test_fires_inside_evening_window_when_not_yet_run(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    fire, reason = eod.should_fire_now("2026-07-07", 18)
    assert fire is True, reason
    fire, _ = eod.should_fire_now("2026-07-07", 23)
    assert fire is True


def test_skips_before_earliest_hour(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    fire, reason = eod.should_fire_now("2026-07-07", 17)
    assert fire is False
    assert "before window" in reason


def test_skips_after_latest_hour(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    monkeypatch.setenv(eod.ENV_LATEST, "20")
    fire, reason = eod.should_fire_now("2026-07-07", 21)
    assert fire is False
    assert "after window" in reason


def test_respects_custom_window(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    monkeypatch.setenv(eod.ENV_HOUR, "20")
    monkeypatch.setenv(eod.ENV_LATEST, "22")
    fire, _ = eod.should_fire_now("2026-07-07", 20)  # newly inside
    assert fire is True
    fire, _ = eod.should_fire_now("2026-07-07", 19)  # still below
    assert fire is False
    fire, _ = eod.should_fire_now("2026-07-07", 22)  # at cap (excl)
    assert fire is False


def test_skips_when_master_disabled(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    monkeypatch.setenv(eod.ENV_ENABLED, "0")
    fire, reason = eod.should_fire_now("2026-07-07", 19)
    assert fire is False
    assert "disabled" in reason


def test_skips_when_review_already_ran_today(monkeypatch, isolated_root):
    _clear_env(monkeypatch)
    _write_review(isolated_root, "2026-07-07")  # today's review already exists
    fire, reason = eod.should_fire_now("2026-07-07", 19)
    assert fire is False
    assert "already ran" in reason
    # a DIFFERENT day is not suppressed by today's file
    fire, _ = eod.should_fire_now("2026-07-08", 19)
    assert fire is True
