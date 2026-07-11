"""Morning brief ECONOMICS section — best-effort render of ROI + arbitration."""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from backend import morning


TODAY = _dt.date.today()


def test_render_economics_omitted_when_no_data(monkeypatch, tmp_path):
    # Redirect every source to empty tmp fixtures -> zero rollup + no
    # arbitration history -> section omitted entirely.
    monkeypatch.setenv("SAMUS_OUTREACH_CONVERSIONS_LOG", str(tmp_path / "c.jsonl"))
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "s.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "v.jsonl"))
    monkeypatch.setenv("SAMUS_LLM_BUDGET_PATH", str(tmp_path / "b.json"))
    monkeypatch.setenv("SAMUS_ROI_ROLLUP_PATH", str(tmp_path / "r.json"))
    monkeypatch.setenv("DDB_PORTFOLIO_SNAPSHOTS_TABLE", "")
    monkeypatch.setenv("SAMUS_ARBITRATION_LOG", str(tmp_path / "a.jsonl"))
    assert morning._render_economics(TODAY) == []


def test_render_economics_shows_roi_and_top10_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_COLOR", "1")

    def fake_get_rollup(day=None, **kw):
        return {
            "day": str(day), "revenue_usd": 500.0, "cost_usd": 120.0,
            "net_usd": 380.0,
            "by_channel": {
                "email": {"revenue_usd": 500.0, "cost_usd": 20.0, "net_usd": 480.0},
                "llm": {"revenue_usd": 0.0, "cost_usd": 100.0, "net_usd": -100.0},
            },
        }

    ranked = [
        {
            "rank": i, "action": "call_follow_up", "target_id": f"p_{i}",
            "ev_usd": 1000.0 - i, "probability": 0.4, "priority": 900.0 - i,
            "rationale": "outreach sent 2d ago — call due",
        }
        for i in range(1, 13)
    ]

    monkeypatch.setattr("backend.finance.roi.get_rollup", fake_get_rollup)
    monkeypatch.setattr(
        "backend.strategy.arbiter.latest_arbitration",
        lambda: {"day": str(TODAY), "best_channel": "email", "ranked": ranked},
    )

    lines = morning._render_economics(TODAY)
    text = "\n".join(lines)
    assert "ECONOMICS" in text
    assert "$500.00" in text          # revenue
    assert "email" in text
    assert "bandit-preferred channel: email" in text
    # Top 10 only — ranks 11 and 12 are cut.
    assert "p_10" in text and "p_11" not in text and "p_12" not in text
    assert "outreach sent 2d ago" in text


def test_render_economics_survives_broken_sources(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr("backend.finance.roi.get_rollup", boom)
    monkeypatch.setattr("backend.strategy.arbiter.latest_arbitration", boom)
    assert morning._render_economics(TODAY) == []


def test_render_briefing_call_site_is_wired():
    """render_briefing calls _render_economics (full render needs live
    finance registries that aren't present in the test env — the section
    function itself is covered above)."""
    import inspect
    src = inspect.getsource(morning.render_briefing)
    assert "_render_economics(today)" in src
