"""Tests for backend.social.video.voiceover — SRT building + fail-closed guards.

The edge-tts network/synthesis path is not exercised (it needs the package + a
network call); these cover the deterministic SRT builder, the timestamp
formatter, and the empty-input guard, which carry the real logic.
"""
from __future__ import annotations

import pytest

from backend.social.video.voiceover import VoiceoverError, _build_srt, _ts, synthesize

_TPS = 10_000_000  # 100-ns ticks per second


def _word(text: str, offset_s: float, dur_s: float) -> dict:
    return {
        "type": "WordBoundary",
        "text": text,
        "offset": int(offset_s * _TPS),
        "duration": int(dur_s * _TPS),
    }


def test_ts_formats_srt_timestamp():
    assert _ts(0) == "00:00:00,000"
    assert _ts(1.5) == "00:00:01,500"
    assert _ts(3661.789) == "01:01:01,789"


def test_build_srt_groups_words_into_captions():
    words = [_word(f"w{i}", i * 0.5, 0.5) for i in range(9)]
    srt, end = _build_srt(words)
    # 9 words / 7-per-caption -> 2 cues.
    assert srt.count("-->") == 2
    assert srt.startswith("1\n")
    assert "2\n" in srt
    assert end == pytest.approx(4.5, abs=0.01)


def test_build_srt_empty_returns_blank():
    srt, end = _build_srt([])
    assert srt == ""
    assert end == 0.0


def test_synthesize_empty_text_raises():
    with pytest.raises(VoiceoverError):
        synthesize("   ", voice="en-US-AriaNeural", out_dir="/tmp/does-not-matter")
