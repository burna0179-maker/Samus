"""Tests for backend.social.video.compose — pure helpers + fail-closed guards.

The MoviePy render path is not exercised (needs moviepy + ffmpeg); these cover
the SRT parser, music resolver, and the early ComposeError guards that run
*before* moviepy is imported.
"""
from __future__ import annotations

import pytest

from backend.social.video.compose import (
    ComposeError,
    _parse_srt,
    compose_reel,
    resolve_music_path,
)

_SRT = """1
00:00:00,000 --> 00:00:02,500
First caption here

2
00:00:02,500 --> 00:00:05,000
Second caption line
"""


def test_parse_srt_extracts_cues():
    cues = _parse_srt(_SRT)
    assert len(cues) == 2
    assert cues[0] == (0.0, 2.5, "First caption here")
    assert cues[1][2] == "Second caption line"


def test_parse_srt_tolerates_blank_and_dotted():
    cues = _parse_srt("1\n00:00:01.000 --> 00:00:02.000\nHi\n")
    assert cues == [(1.0, 2.0, "Hi")]


def test_parse_srt_empty():
    assert _parse_srt("") == []


def test_compose_no_footage_raises():
    with pytest.raises(ComposeError):
        compose_reel([], "voice.mp3", "subs.srt", out_path="out.mp4")


def test_compose_missing_voiceover_raises(tmp_path):
    with pytest.raises(ComposeError):
        compose_reel(["shot.png"], str(tmp_path / "nope.mp3"), "subs.srt",
                     out_path=str(tmp_path / "out.mp4"))


def test_resolve_music_path_empty_and_missing(tmp_path):
    assert resolve_music_path("") == ""
    assert resolve_music_path(str(tmp_path / "nope")) == ""


def test_resolve_music_path_picks_first_sorted(tmp_path):
    (tmp_path / "b.mp3").write_bytes(b"x")
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignore")
    assert resolve_music_path(str(tmp_path)).endswith("a.mp3")
