"""Tests for the Instagram Reels (video_url) path in backend.social.adapters.

The httpx client is faked — no network. Covers the two-step container->publish
flow with the async processing poll, plus the container-error fail-closed path.
"""

from __future__ import annotations

import importlib

from backend.social.models import PlannedPost


def _reel_post() -> PlannedPost:
    return PlannedPost(
        week=1,
        day="Tue",
        platform="instagram",
        fmt="ig_reel",
        pipeline_fn="educate",
        theme="Authority",
        cluster="AI visibility",
        body="A specific, useful tactic for AI visibility.",
        stake_sentence="I personally reviewed this and vouch for it.",
    )


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    """Records calls; returns canned responses keyed by URL substring."""

    def __init__(self, *, status_sequence=("FINISHED",), **_):
        self.calls = []
        self._status_seq = list(status_sequence)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, data=None, json=None):
        self.calls.append(("POST", url, data or json))
        if url.endswith("/media"):
            return _Resp(200, {"id": "CREATION_1"})
        if url.endswith("/media_publish"):
            return _Resp(200, {"id": "MEDIA_42"})
        return _Resp(400, {})

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        code = self._status_seq.pop(0) if self._status_seq else "FINISHED"
        return _Resp(200, {"status_code": code})


def _reload_live(monkeypatch):
    monkeypatch.setenv("SAMUS_SOCIAL_DRY_RUN", "false")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "acct123")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    import backend.outreach.social_adapter as outreach_mod

    importlib.reload(outreach_mod)
    import backend.social.adapters as mod

    importlib.reload(mod)
    return mod


def test_reel_publish_happy_path(monkeypatch, tmp_path):
    mod = _reload_live(monkeypatch)
    monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(tmp_path / "p.jsonl"))

    made = {}

    def factory(*a, **k):
        made["c"] = _FakeClient(status_sequence=["IN_PROGRESS", "FINISHED"])
        return made["c"]

    monkeypatch.setattr(mod.httpx, "Client", factory)

    res = mod.dispatch_post(_reel_post(), video_url="https://cdn.example.com/reel.mp4")
    assert res.sent is True
    assert res.post_id == "MEDIA_42"
    # The container create used REELS + video_url, and we polled then published.
    create = next(c for c in made["c"].calls if c[0] == "POST" and c[1].endswith("/media"))
    assert create[2]["media_type"] == "REELS"
    assert create[2]["video_url"] == "https://cdn.example.com/reel.mp4"
    assert any(c[0] == "GET" for c in made["c"].calls)
    assert any(c[1].endswith("/media_publish") for c in made["c"].calls)


def test_reel_container_error_fails_closed(monkeypatch, tmp_path):
    mod = _reload_live(monkeypatch)
    monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(tmp_path / "p.jsonl"))
    monkeypatch.setattr(mod.httpx, "Client", lambda *a, **k: _FakeClient(status_sequence=["ERROR"]))

    res = mod.dispatch_post(_reel_post(), video_url="https://cdn.example.com/reel.mp4")
    assert res.sent is False
    assert res.error == "instagram_container_error"


def test_reel_without_media_refuses(monkeypatch, tmp_path):
    mod = _reload_live(monkeypatch)
    monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(tmp_path / "p.jsonl"))
    res = mod.dispatch_post(_reel_post())  # no image_url, no video_url
    assert res.sent is False
    assert res.error == "instagram_media_required"
