"""Action calendar loader + bucket math."""

from __future__ import annotations

from datetime import date


def test_missing_file_returns_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(tmp_path / "nope.yaml"))
    from backend.finance.actions import load_registry

    reg, loaded = load_registry()
    assert loaded is False
    assert reg.actions == []


def test_bucketing_with_pinned_today(tmp_path, monkeypatch):
    p = tmp_path / "actions.yaml"
    p.write_text(
        "actions:\n"
        "  - {id: A1, when: TODAY, due_date: 2026-05-15, action: 'overdue from yesterday-style', status: open}\n"
        "  - {id: A2, when: TODAY, due_date: 2026-05-10, action: 'overdue', status: open}\n"
        "  - {id: A3, when: TODAY, due_date: 2026-05-15, action: 'due today 1', status: open}\n"
        "  - {id: A4, when: TODAY, due_date: 2026-05-15, action: 'due today 2', status: open}\n"
        "  - {id: A5, when: THIS_WEEK, due_date: 2026-05-18, action: 'this week', status: open}\n"
        "  - {id: A6, when: THIS_WEEK, due_date: 2026-05-22, action: 'this week edge', status: open}\n"
        "  - {id: A7, when: THIS_WEEK, due_date: 2026-05-30, action: 'next week', status: open}\n"
        "  - {id: A8, when: TODAY, due_date: 2026-05-10, action: 'done overdue', status: done, completed_date: 2026-05-12}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(p))
    from backend.finance.actions import load_registry, summarize

    reg, loaded = load_registry()
    s = summarize(
        reg, loaded, ts="2026-05-15T00:00:00Z", today=date(2026, 5, 15), week_window_days=7
    )
    assert s.open_total == 7
    assert s.overdue_count == 1  # only A2 (A1 is exactly today)
    assert s.due_today_count == 3  # A1, A3, A4
    assert s.due_this_week_count == 2  # A5, A6 (A7 is outside 7-day window)


def test_done_actions_excluded(tmp_path, monkeypatch):
    p = tmp_path / "actions.yaml"
    p.write_text(
        "actions:\n"
        "  - {id: D1, when: TODAY, due_date: 2026-05-15, action: 'x', status: done, completed_date: 2026-05-14}\n"
        "  - {id: D2, when: TODAY, due_date: 2026-05-15, action: 'y', status: deferred}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(p))
    from backend.finance.actions import load_registry, summarize

    reg, loaded = load_registry()
    s = summarize(reg, loaded, ts="t", today=date(2026, 5, 15))
    assert s.open_total == 0
    assert s.due_today_count == 0


def test_overdue_sort_oldest_first(tmp_path, monkeypatch):
    p = tmp_path / "actions.yaml"
    p.write_text(
        "actions:\n"
        "  - {id: A1, when: TODAY, due_date: 2026-05-10, action: 'older', status: open}\n"
        "  - {id: A2, when: TODAY, due_date: 2026-05-05, action: 'oldest', status: open}\n"
        "  - {id: A3, when: TODAY, due_date: 2026-05-14, action: 'newer', status: open}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(p))
    from backend.finance.actions import load_registry, summarize

    reg, loaded = load_registry()
    s = summarize(reg, loaded, ts="t", today=date(2026, 5, 15))
    assert [a.id for a in s.overdue_actions] == ["A2", "A1", "A3"]
