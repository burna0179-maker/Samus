"""Tests for backend.social.calendar — themes, weekly rhythm, planning."""

from __future__ import annotations

import json

import pytest

from backend.social.calendar import (
    MONTHLY_THEMES,
    WEEKLY_RHYTHM,
    convert_over_ceiling,
    load_plan,
    mix_distribution,
    persist_plan,
    plan_month,
    theme_for_month,
)


def test_four_themes_defined():
    assert len(MONTHLY_THEMES) == 4
    assert all(len(t.weekly_focus) == 4 for t in MONTHLY_THEMES)


def test_theme_for_month_cycles():
    assert theme_for_month(1).month == 1
    assert theme_for_month(4).month == 4
    # Month 5 wraps back to theme 1.
    assert theme_for_month(5).name == theme_for_month(1).name
    assert theme_for_month(9).name == theme_for_month(1).name


def test_theme_for_month_rejects_non_positive():
    with pytest.raises(ValueError):
        theme_for_month(0)


def test_weekly_rhythm_has_eighteen_slots():
    assert len(WEEKLY_RHYTHM) == 18
    days = {s.day for s in WEEKLY_RHYTHM}
    assert days == {"Mon", "Tue", "Wed", "Thu", "Fri"}


def test_plan_month_post_count():
    plan = plan_month(1, "AI prospecting", weeks=4)
    assert len(plan.posts) == 72  # 4 weeks x 18 slots
    assert plan.cluster == "AI prospecting"
    assert all(p.status == "planned" for p in plan.posts)


def test_plan_month_brief_uses_week_focus():
    plan = plan_month(1, "AI prospecting", weeks=1)
    theme = theme_for_month(1)
    focus = theme.weekly_focus[0]
    assert any(focus in p.brief for p in plan.posts)


def test_mix_distribution_sums_to_one():
    plan = plan_month(2, "methodology")
    mix = mix_distribution(plan.posts)
    assert abs(sum(mix.values()) - 1.0) < 0.01
    assert set(mix) == {"educate", "prove", "engage", "convert"}


def test_convert_stays_under_ceiling():
    plan = plan_month(1, "cluster")
    assert convert_over_ceiling(plan.posts) is False
    assert mix_distribution(plan.posts)["convert"] <= 0.15


def test_mix_distribution_empty():
    assert mix_distribution([]) == {
        "educate": 0.0,
        "prove": 0.0,
        "engage": 0.0,
        "convert": 0.0,
    }


def test_persist_and_load_roundtrip(tmp_path):
    plan = plan_month(3, "case studies", weeks=2)
    path = tmp_path / "cal.jsonl"
    n = persist_plan(plan, path)
    assert n == 36  # 2 weeks x 18

    # Header line + one line per post.
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 37
    header = json.loads(lines[0])
    assert header["_kind"] == "social_calendar"
    assert header["post_count"] == 36
    assert "mix" in header

    reloaded = load_plan(path)
    assert reloaded.month == 3
    assert reloaded.cluster == "case studies"
    assert len(reloaded.posts) == 36
    assert reloaded.posts[0].theme == plan.posts[0].theme


def test_load_plan_empty_raises(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_plan(empty)
