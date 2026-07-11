"""Promotion ledger — pipeline memory of already-promoted prospects (2026-07-03).

Covers the operator-reported failure: the cumulative geo-ring re-promoted the
same businesses as "new prospects" every day because nothing remembered who
had already entered the pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.prospecting.promotion_ledger import (
    record_promotions,
    recently_promoted_ids,
    recycle_days,
)


@pytest.fixture()
def state_root(monkeypatch, tmp_path):
    from backend.common import storage

    monkeypatch.setattr(storage, "root", lambda: tmp_path)
    return tmp_path


def _p(pid: str, company: str = "Acme", email: str = "a@x.com"):
    return SimpleNamespace(prospect_id=pid, company_name=company, owner_email=email)


def test_record_then_recall(state_root):
    assert record_promotions([_p("p1"), _p("p2")]) == 2
    assert recently_promoted_ids() == {"p1", "p2"}


def test_cooldown_expiry_requalifies(state_root):
    # A promotion older than the window is forgotten — that lapse IS the
    # recycle pass.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    path = state_root / "daily_calls" / "promoted_prospects.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": old_ts, "prospect_id": "aged"})
        + "\n"
        + json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "prospect_id": "recent"})
        + "\n",
        encoding="utf-8",
    )
    assert recently_promoted_ids(within_days=30) == {"recent"}


def test_env_recycle_days(monkeypatch, state_root):
    monkeypatch.setenv("SAMUS_PROSPECT_RECYCLE_DAYS", "7")
    assert recycle_days() == 7
    ts8 = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    path = state_root / "daily_calls" / "promoted_prospects.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": ts8, "prospect_id": "p8"}) + "\n", encoding="utf-8")
    assert recently_promoted_ids() == set()  # 8 days old > 7-day window


def test_garbage_rows_and_missing_file_degrade_to_empty(state_root):
    assert recently_promoted_ids() == set()
    path = state_root / "daily_calls" / "promoted_prospects.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('not-json\n{"ts": "garbage", "prospect_id": "x"}\n', encoding="utf-8")
    assert recently_promoted_ids() == set()


def test_blank_prospect_ids_skipped(state_root):
    assert record_promotions([_p(""), _p("real")]) == 1
    assert recently_promoted_ids() == {"real"}


# ---------------------------------------------------------------------------
# Recycle context — returning prospects carry prior-touch history (2026-07-03)
# ---------------------------------------------------------------------------
def test_ever_promoted_ids_ignores_cooldown(state_root):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    path = state_root / "daily_calls" / "promoted_prospects.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": old_ts, "prospect_id": "ancient"}) + "\n", encoding="utf-8")
    from backend.prospecting.promotion_ledger import ever_promoted_ids

    assert ever_promoted_ids() == {"ancient"}
    assert recently_promoted_ids(within_days=30) == set()


def test_touch_summary_digest_shape(monkeypatch):
    from backend.prospecting import touch_context as tc

    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(base, path, timeout=0):
        if "/crm/conversations" in path:
            return _Resp(
                200,
                {
                    "conversations": [
                        {
                            "channel": "email",
                            "ended_at": "2026-06-15T10:00:00Z",
                            "outcome": "no_reply",
                            "summary": "Sent SEO audit note",
                        },
                        {
                            "channel": "call",
                            "ended_at": "2026-06-20T18:00:00Z",
                            "outcome": "voicemail",
                            "summary": "",
                        },
                    ]
                },
            )
        return _Resp(200, {"last_outcome": "follow_up", "updated_at": "2026-06-21T09:00:00Z"})

    import backend.common.http_client as hc

    monkeypatch.setattr(tc, "_crm_url", lambda: "http://x")
    monkeypatch.setattr(hc, "signed_get_json_sync", fake_get)
    out = tc.build_touch_summary("p1")
    assert out.startswith("prior touches: ")
    assert "email 6/15" in out and "no_reply" in out
    assert "call 6/20" in out and "voicemail" in out
    assert "follow_up" in out
    assert len(out) <= 400


def test_touch_summary_degrades_to_empty(monkeypatch):
    from backend.prospecting import touch_context as tc

    import backend.common.http_client as hc

    def boom(*a, **k):
        raise RuntimeError("crm down")

    monkeypatch.setattr(hc, "signed_get_json_sync", boom)
    assert tc.build_touch_summary("p1") == ""
    assert tc.build_touch_summary("") == ""


def test_recycled_voicemail_gets_followup_weave():
    from types import SimpleNamespace

    from backend.cash_engine.production_actuator import _default_draft_voicemail

    # Direct template text on the prospect so no callsheet import is needed.
    p = SimpleNamespace(
        prospect_id="pr_x",
        recycled="true",
        prior_touch_summary="prior touches: email 6/15 no_reply",
        callsheet_voicemail="Hi, this is [NAME] from HustleForge. Your site grades an F.",
    )
    import backend.common.storage as storage
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    old_root = storage.root
    storage.root = lambda: root
    try:
        out = _default_draft_voicemail(p)
        assert out.get("drafted") is True
        text = Path(out["path"]).read_text(encoding="utf-8")
        assert "Following up on my earlier message" in text
        assert text.count("Hi,") == 1  # single greeting — clause is woven, not prefixed
        assert "[operator context" in text and "email 6/15" in text
    finally:
        storage.root = old_root


def test_recycled_stake_sentence_is_followup_framed():
    from backend.outreach.morning_batch import _stake_sentence

    row = {
        "company_name": "Acme Dental",
        "city": "Yuba City",
        "recycled": "true",
        "last_contact_at": "2026-06-15T10:00:00Z",
        "security_grade": "F",
    }
    s = _stake_sentence(row)
    assert "follow up" in s.lower()
    assert "2026-06-15" in s
    assert len(s) <= 280
    # Non-recycled rows keep the original cold framing.
    cold = _stake_sentence({"company_name": "Acme Dental", "security_grade": "F"})
    assert "follow up" not in cold.lower()
