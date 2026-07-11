"""Tests for backend.social.adapters — unified dispatch (DRY-RUN by default).

No live network call is ever made. Live-path tests assert fail-closed refusals
(missing credentials / missing stake sentence) which never touch the network.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from backend.social.models import PlannedPost

_CRED_KEYS = (
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_BUSINESS_ACCOUNT_ID",
    "X_BEARER_TOKEN",
    "LINKEDIN_ACCESS_TOKEN",
    "LINKEDIN_AUTHOR_URN",
    "FACEBOOK_PAGE_TOKEN",
    "FACEBOOK_PAGE_ID",
)


def _reload(monkeypatch, *, dry_run: str = "true", ledger: Path | None = None):
    """Reload outreach + social adapters under controlled env. Reloading
    outreach first so social.adapters re-binds the reloaded send_post."""
    monkeypatch.setenv("SAMUS_SOCIAL_DRY_RUN", dry_run)
    if ledger is not None:
        monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(ledger))
    for key in _CRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    import backend.outreach.social_adapter as outreach_mod

    importlib.reload(outreach_mod)
    import backend.social.adapters as mod

    importlib.reload(mod)
    return mod


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _post(
    platform: str, *, body: str = "A useful, specific insight about pipelines.", stake: str = ""
) -> PlannedPost:
    return PlannedPost(
        week=1,
        day="Mon",
        platform=platform,  # type: ignore[arg-type]
        fmt="li_text" if platform == "linkedin" else "x_tweet",
        pipeline_fn="educate",
        theme="Foundation",
        cluster="AI visibility",
        brief="brief",
        body=body,
        stake_sentence=stake,
    )


# --- DRY-RUN (default) ------------------------------------------------------


def test_dispatch_dry_run_default(monkeypatch, tmp_path):
    ledger = tmp_path / "posts.jsonl"
    mod = _reload(monkeypatch, dry_run="true", ledger=ledger)
    for platform in ("linkedin", "instagram", "x", "facebook"):
        res = mod.dispatch_post(_post(platform))
        assert res.sent is True
        assert res.dry_run is True
        assert res.platform == platform
        assert res.post_id.startswith("dry-")
    records = _read_ledger(ledger)
    assert len(records) == 4
    assert {r["platform"] for r in records} == {"linkedin", "instagram", "x", "facebook"}


def test_dispatch_plan_dry_run_with_limit(monkeypatch, tmp_path):
    mod = _reload(monkeypatch, dry_run="true", ledger=tmp_path / "p.jsonl")
    posts = [_post("x") for _ in range(5)]
    results = mod.dispatch_plan(posts, limit=2)
    assert len(results) == 2
    assert all(r.dry_run for r in results)


# --- LIVE path fail-closed refusals (no network) ----------------------------


def test_live_instagram_without_token_refuses(monkeypatch, tmp_path):
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    res = mod.dispatch_post(
        _post("instagram", stake="I personally reviewed this and vouch for it."),
        image_url="https://example.com/x.png",
    )
    assert res.sent is False
    assert res.error == "instagram_token_unset"


def test_live_x_without_token_refuses(monkeypatch, tmp_path):
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    res = mod.dispatch_post(_post("x", stake="I personally reviewed this and vouch for it."))
    assert res.sent is False
    assert res.error == "x_token_unset"


def test_live_requires_stake_sentence(monkeypatch, tmp_path):
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    res = mod.dispatch_post(_post("instagram", stake=""))
    assert res.sent is False
    assert res.error == "stake_sentence_required"


def test_live_empty_body_refused(monkeypatch, tmp_path):
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    res = mod.dispatch_post(
        _post("x", body="", stake="I personally reviewed this and vouch for it.")
    )
    assert res.sent is False
    assert res.error == "empty_body"


def test_live_linkedin_delegates_to_outreach(monkeypatch, tmp_path):
    """LinkedIn routes through the outreach adapter, which (no token, live)
    refuses with its own error — proving delegation, no network call."""
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    res = mod.dispatch_post(_post("linkedin", stake="I personally reviewed this and vouch for it."))
    assert res.sent is False
    assert res.platform == "linkedin"
    assert res.error == "linkedin_token_unset"


def test_live_facebook_delegates_to_outreach(monkeypatch, tmp_path):
    """Facebook routes through the outreach adapter, which (no token, live)
    refuses with its own error — proving delegation, no network call."""
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    fb_post = PlannedPost(
        week=1,
        day="Mon",
        platform="facebook",
        fmt="fb_post",
        pipeline_fn="educate",
        theme="Foundation",
        cluster="AI visibility",
        brief="brief",
        body="Helpful community post about pipeline optimization.",
        stake_sentence="I personally reviewed this and vouch for it.",
    )
    res = mod.dispatch_post(fb_post)
    assert res.sent is False
    assert res.platform == "facebook"
    assert res.error == "facebook_token_unset"


def test_live_moderation_blocks_banned_phrase(monkeypatch, tmp_path):
    mod = _reload(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")
    res = mod.dispatch_post(
        _post(
            "x",
            body="We offer guaranteed returns on every campaign.",
            stake="I personally reviewed this and vouch for it.",
        )
    )
    assert res.sent is False
    assert res.error.startswith("moderation_blocked")
