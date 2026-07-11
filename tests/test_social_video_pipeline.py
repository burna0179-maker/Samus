"""Tests for backend.social.video.pipeline — orchestration, dormancy, fail-close.

Voiceover / footage / compose are monkeypatched, so the pipeline is exercised
end-to-end with no edge-tts, no media spend, and no ffmpeg. State writes are
redirected to a tmp dir via SAMUS_STATE_ROOT.
"""

from __future__ import annotations

from types import SimpleNamespace

import backend.social.video.compose as compose_mod
import backend.social.video.footage as footage_mod
import backend.social.video.voiceover as voiceover_mod
from backend.social.models import BlogInput
from backend.social.video.pipeline import produce_reel


def _settings(**over) -> SimpleNamespace:
    base = dict(
        social_reel_enabled=True,
        social_reel_aspect="9:16",
        social_reel_max_segments=3,
        social_reel_footage_mode="image",
        social_reel_tts_voice="en-US-AriaNeural",
        social_reel_music_dir="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _blog() -> BlogInput:
    return BlogInput(
        title="The 44% rule",
        summary="44% of citations come early.",
        key_points=["Front-load the answer", "Add an FAQ block"],
    )


def _wire_success(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fake_vo(text, *, voice, out_dir, stem="voiceover"):
        return SimpleNamespace(
            mp3_path=str(tmp_path / "voiceover.mp3"),
            srt_path=str(tmp_path / "voiceover.srt"),
            duration_s=12.5,
            word_count=30,
        )

    def fake_footage(segments, *, settings, out_dir, video_approved=False):
        paths = [str(tmp_path / f"shot_{i}.png") for i in range(len(segments))]
        return paths, {
            "status": "ok",
            "generated": len(paths),
            "planned": len(segments),
            "skipped": [],
            "spent_usd": 0.04 * len(paths),
        }

    def fake_compose(footage, mp3, srt, *, out_path, aspect, music_path):
        return str(out_path)

    monkeypatch.setattr(voiceover_mod, "synthesize", fake_vo)
    monkeypatch.setattr(footage_mod, "generate_segment_footage", fake_footage)
    monkeypatch.setattr(compose_mod, "compose_reel", fake_compose)
    monkeypatch.setattr(compose_mod, "resolve_music_path", lambda d: "")


def test_disabled_returns_dormant(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    res = produce_reel(_blog(), settings=_settings(social_reel_enabled=False), use_llm=False)
    assert res.ok is False
    assert res.status == "disabled"
    assert res.spent_usd == 0.0


def test_full_pipeline_success(monkeypatch, tmp_path):
    _wire_success(monkeypatch, tmp_path)
    res = produce_reel(_blog(), settings=_settings(), use_llm=False)
    assert res.ok is True
    assert res.status == "ok"
    assert res.mp4_path.endswith("reel.mp4")
    assert res.srt_path.endswith(".srt")
    assert res.duration_s == 12.5
    assert res.segments_generated >= 1
    assert res.spent_usd > 0


def test_voiceover_failure_is_fail_closed(monkeypatch, tmp_path):
    _wire_success(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise voiceover_mod.VoiceoverError("edge-tts not installed")

    monkeypatch.setattr(voiceover_mod, "synthesize", boom)
    res = produce_reel(_blog(), settings=_settings(), use_llm=False)
    assert res.ok is False
    assert res.status == "error"
    assert "voiceover" in res.error


def test_no_footage_is_fail_closed(monkeypatch, tmp_path):
    _wire_success(monkeypatch, tmp_path)
    monkeypatch.setattr(
        footage_mod,
        "generate_segment_footage",
        lambda *a, **k: (
            [],
            {"status": "no_api_key", "skipped": ["all:no_api_key"], "spent_usd": 0.0},
        ),
    )
    res = produce_reel(_blog(), settings=_settings(), use_llm=False)
    assert res.ok is False
    assert "footage" in res.error


def test_compose_failure_is_fail_closed(monkeypatch, tmp_path):
    _wire_success(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise compose_mod.ComposeError("ffmpeg missing")

    monkeypatch.setattr(compose_mod, "compose_reel", boom)
    res = produce_reel(_blog(), settings=_settings(), use_llm=False)
    assert res.ok is False
    assert "compose" in res.error
    # footage was generated, so spend is still reported even though compose failed.
    assert res.spent_usd > 0
