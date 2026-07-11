"""Tests for backend.social.video.footage — budget-metered AI footage.

All paid calls (media_gen + MediaBudgetStore) are monkeypatched; no network and
no real spend. Footage files are written to a tmp dir.
"""

from __future__ import annotations

from types import SimpleNamespace

import backend.common.media_budget as budget_mod
import backend.website.media_gen as media_gen
from backend.social.video.footage import generate_segment_footage
from backend.social.video.models import ReelSegment


def _settings(**over) -> SimpleNamespace:
    base = dict(
        gemini_api_key="test-key",
        media_daily_dollar_cap=2.0,
        social_reel_aspect="9:16",
        social_reel_footage_mode="image",
        social_reel_video_enabled=False,
        media_video_requires_approval=True,
        media_image_cost_usd=0.04,
        media_video_cost_usd=1.50,
        website_media_image_model="gemini-2.5-flash-image",
        website_media_video_model="veo-3.1-fast-generate-preview",
        website_media_video_resolution="720p",
        website_media_video_seconds="8",
        website_media_people_directive="Diverse cast.",
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeBudget:
    """Allows spend while cap not exhausted; records cumulatively."""

    def __init__(self, cap_usd, **_):
        self.cap = float(cap_usd)
        self.spent = 0.0

    def can_spend(self, est):
        if self.cap <= 0:
            return SimpleNamespace(allowed=False, reason="media_cap_zero")
        if self.spent + est > self.cap:
            return SimpleNamespace(allowed=False, reason="daily_media_cap_exceeded")
        return SimpleNamespace(allowed=True, reason="ok")

    def record(self, actual):
        self.spent += actual


def _segments(n=3, is_video=False):
    return [
        ReelSegment(narration=f"line {i}", visual_prompt=f"shot {i}", is_video=is_video)
        for i in range(n)
    ]


def _patch(monkeypatch, *, image_ok=True):
    monkeypatch.setattr(budget_mod, "MediaBudgetStore", _FakeBudget)

    def fake_image(prompt, **kw):
        if not image_ok:
            raise media_gen.MediaGenError("boom")
        return b"\x89PNG-bytes"

    monkeypatch.setattr(media_gen, "generate_image", fake_image)


def test_no_api_key_spends_nothing(monkeypatch, tmp_path):
    paths, report = generate_segment_footage(
        _segments(),
        settings=_settings(gemini_api_key=""),
        out_dir=tmp_path,
    )
    assert paths == []
    assert report["status"] == "no_api_key"
    assert report["spent_usd"] == 0.0


def test_generates_one_still_per_segment(monkeypatch, tmp_path):
    _patch(monkeypatch)
    paths, report = generate_segment_footage(_segments(3), settings=_settings(), out_dir=tmp_path)
    assert len(paths) == 3
    assert report["generated"] == 3
    assert all(p.endswith(".png") for p in paths)
    assert report["spent_usd"] == 0.12  # 3 * 0.04


def test_budget_cap_zero_denies_all(monkeypatch, tmp_path):
    _patch(monkeypatch)
    paths, report = generate_segment_footage(
        _segments(3),
        settings=_settings(media_daily_dollar_cap=0.0),
        out_dir=tmp_path,
    )
    assert paths == []
    assert report["spent_usd"] == 0.0
    assert all("media_cap_zero" in s for s in report["skipped"])


def test_budget_partial_exhaustion_skips_overflow(monkeypatch, tmp_path):
    _patch(monkeypatch)
    # Cap of 0.05 allows exactly one $0.04 still; the rest are skipped.
    paths, report = generate_segment_footage(
        _segments(3),
        settings=_settings(media_daily_dollar_cap=0.05),
        out_dir=tmp_path,
    )
    assert len(paths) == 1
    assert any("daily_media_cap_exceeded" in s for s in report["skipped"])


def test_video_segment_degrades_to_still_without_approval(monkeypatch, tmp_path):
    _patch(monkeypatch)
    s = _settings(social_reel_footage_mode="video", social_reel_video_enabled=True)
    paths, report = generate_segment_footage(
        _segments(1, is_video=True), settings=s, out_dir=tmp_path, video_approved=False
    )
    # Degraded to a still (image cost), and a note recorded.
    assert len(paths) == 1
    assert paths[0].endswith(".png")
    assert any("video_requires_approval" in s for s in report["skipped"])
    assert report["spent_usd"] == 0.04


def test_image_failure_is_skipped_not_raised(monkeypatch, tmp_path):
    _patch(monkeypatch, image_ok=False)
    paths, report = generate_segment_footage(_segments(2), settings=_settings(), out_dir=tmp_path)
    assert paths == []
    assert report["status"] == "empty"
    assert len(report["skipped"]) == 2
