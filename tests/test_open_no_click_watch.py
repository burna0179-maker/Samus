"""Open-no-click nudge watcher — register, tick, close, fire-on-dwell."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.common.config as config_mod
import backend.outreach.open_no_click_watch as watch


def _fake_get_settings(*, nudge_enabled: bool, dwell_h: int = 24):
    def _gs():
        return SimpleNamespace(
            outreach_open_no_click_nudge_enabled=nudge_enabled,
            outreach_open_no_click_dwell_hours=dwell_h,
        )

    _gs.cache_clear = lambda: None
    return _gs


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Fresh artifact root + engagement dir + flag-off settings."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    eng = tmp_path / "engagement"
    eng.mkdir()
    monkeypatch.setenv("SAMUS_ENGAGEMENT_DIR", str(eng))
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(nudge_enabled=False))
    return SimpleNamespace(root=tmp_path, engagement_dir=eng)


def _write_engagement(eng_dir: Path, day: str, entries: list[dict]):
    f = eng_dir / f"engagement_{day}.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _register_kelly(sent="2026-06-30T17:14:47Z"):
    return watch.register(
        prospect_id="pr_kelly",
        email="kellyzrealtor@gmail.com",
        sent_at_iso=sent,
        subject="Kelly — your receptionist build, scoped",
        buy_url="https://buy.stripe.com/cNi14m74C1ay9Ke0ja8so0c",
        message_id="msg_abc",
        company="Kelly Zimmerman, eXp Realty",
        campaign_id="kelly_workflow_rescue",
    )


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_writes_record(isolated):
    out = _register_kelly()
    assert out["registered"] is True
    recs = watch._read()
    assert len(recs) == 1
    r = recs[0]
    assert r["prospect_id"] == "pr_kelly"
    assert r["nudged"] is False
    assert r["closed"] is False
    assert r["first_open_at"] is None
    assert r["first_click_at"] is None


def test_register_is_idempotent(isolated):
    assert _register_kelly()["registered"] is True
    again = _register_kelly()
    assert again["registered"] is False
    assert again["reason"] == "already_watching"
    assert len(watch._read()) == 1


# ---------------------------------------------------------------------------
# tick — signal classification
# ---------------------------------------------------------------------------


def test_tick_no_engagement_yields_no_open(isolated):
    _register_kelly()
    out = watch.tick(now_iso="2026-07-01T17:14:47Z")  # +24h, no events
    assert out == [{"prospect_id": "pr_kelly", "action": "no_open_yet"}]


def test_tick_click_closes_record_without_nudge(isolated):
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
            {
                "ts": "2026-06-30T18:02:00Z",
                "prospect_id": "pr_kelly",
                "signal": "clicked",
                "source": "sendgrid_webhook",
            },
        ],
    )
    out = watch.tick(now_iso="2026-07-01T18:30:00Z")
    assert out == [{"prospect_id": "pr_kelly", "action": "closed_clicked"}]
    r = watch._read()[0]
    assert r["closed"] is True
    assert r["closed_reason"] == "clicked"
    assert r["nudged"] is False


def test_tick_open_within_dwell_no_action(isolated):
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
        ],
    )
    # 12h after the open — well inside the 24h dwell window.
    out = watch.tick(now_iso="2026-07-01T06:00:00Z")
    assert out[0]["action"] == "dwell_not_reached"
    r = watch._read()[0]
    assert r["first_open_at"] == "2026-06-30T18:00:00Z"
    assert r["nudged"] is False


# ---------------------------------------------------------------------------
# tick — dwell crossed, flag posture
# ---------------------------------------------------------------------------


def test_tick_dwell_crossed_flag_off_records_would_nudge(isolated):
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
        ],
    )
    # 25h after open — dwell crossed.
    out = watch.tick(now_iso="2026-07-01T19:00:00Z")
    assert out[0]["action"] == "would_nudge_flag_off"
    r = watch._read()[0]
    assert r["nudged"] is False
    assert r.get("would_nudge_at") == "2026-07-01T19:00:00Z"


def test_tick_dwell_crossed_flag_on_sends_nudge(isolated, monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(nudge_enabled=True))
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
        ],
    )
    sent_calls = []

    def _fake_send_email(**kw):
        sent_calls.append(kw)
        return {
            "message_id": "nudge_msg_id",
            "channel": "email",
            "to": kw["to"],
            "ts": "2026-07-01T19:00:00Z",
        }

    import backend.common.email_backend as eb

    monkeypatch.setattr(eb, "send_email", _fake_send_email)

    out = watch.tick(now_iso="2026-07-01T19:00:00Z")
    assert out[0]["action"] == "nudged"
    assert len(sent_calls) == 1
    sent = sent_calls[0]
    assert sent["to"] == "kellyzrealtor@gmail.com"
    assert sent["subject"].startswith("Re:")
    assert "https://buy.stripe.com" in sent["html_body"]
    assert sent["custom_args"]["touch"] == "open_no_click_nudge"
    r = watch._read()[0]
    assert r["nudged"] is True
    assert r["nudge_message_id"] == "nudge_msg_id"


def test_tick_nudge_is_idempotent(isolated, monkeypatch):
    """A second tick after firing must NOT re-fire."""
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(nudge_enabled=True))
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
        ],
    )
    send_count = {"n": 0}

    def _fake_send_email(**kw):
        send_count["n"] += 1
        return {"message_id": "x", "channel": "email", "to": kw["to"], "ts": "x"}

    import backend.common.email_backend as eb

    monkeypatch.setattr(eb, "send_email", _fake_send_email)

    watch.tick(now_iso="2026-07-01T19:00:00Z")
    watch.tick(now_iso="2026-07-01T20:00:00Z")
    assert send_count["n"] == 1
    out = watch.tick(now_iso="2026-07-01T20:00:00Z")
    assert out[0]["action"] == "already_nudged"


def test_force_fire_overrides_flag_off(isolated, monkeypatch):
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
        ],
    )
    sent: list = []
    import backend.common.email_backend as eb

    monkeypatch.setattr(
        eb,
        "send_email",
        lambda **kw: (
            sent.append(kw) or {"message_id": "x", "channel": "email", "to": kw["to"], "ts": "x"}
        ),
    )
    out = watch.tick(now_iso="2026-07-01T19:00:00Z", dry_run=False)
    assert out[0]["action"] == "nudged"
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# mark_closed — Stripe payment / operator cancel
# ---------------------------------------------------------------------------


def test_mark_closed_cancels_pending_nudge(isolated, monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(nudge_enabled=True))
    _register_kelly()
    _write_engagement(
        isolated.engagement_dir,
        "2026-06-30",
        [
            {
                "ts": "2026-06-30T18:00:00Z",
                "prospect_id": "pr_kelly",
                "signal": "opened",
                "source": "sendgrid_webhook",
            },
        ],
    )
    n = watch.mark_closed(prospect_id="pr_kelly", reason="closed_won")
    assert n == 1
    sent: list = []
    import backend.common.email_backend as eb

    monkeypatch.setattr(
        eb,
        "send_email",
        lambda **kw: (
            sent.append(kw) or {"message_id": "x", "channel": "email", "to": kw["to"], "ts": "x"}
        ),
    )
    out = watch.tick(now_iso="2026-07-01T19:00:00Z")
    assert out == []  # closed records skipped entirely
    assert sent == []


# ---------------------------------------------------------------------------
# Fail-open — engagement dir absent doesn't crash
# ---------------------------------------------------------------------------


def test_tick_with_no_engagement_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_ENGAGEMENT_DIR", str(tmp_path / "does_not_exist"))
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(nudge_enabled=False))
    _register_kelly()
    out = watch.tick(now_iso="2026-07-01T19:00:00Z")
    # No engagement events → no_open_yet, but no crash.
    assert out[0]["action"] == "no_open_yet"
