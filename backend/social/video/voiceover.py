"""Voiceover + subtitles via edge-tts (free Microsoft neural voices).

edge-tts needs no API key. We stream the synthesis so we capture both the audio
bytes (mp3) and the per-word ``WordBoundary`` events, then build an SRT directly
from those timings — so subtitles are deterministic and we avoid both a
faster-whisper model download *and* edge-tts ``SubMaker`` API churn across
versions.

``edge_tts`` is imported lazily: this module imports fine without it; only
:func:`synthesize` requires it, and it fails closed with :class:`VoiceoverError`
when the package is missing or the network call fails.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger("samus.social.video.voiceover")

# WordBoundary offsets/durations are in 100-nanosecond ticks.
_TICKS_PER_SECOND = 10_000_000
_WORDS_PER_CAPTION = 7


class VoiceoverError(Exception):
    """edge-tts is unavailable or synthesis failed."""


@dataclass
class Voiceover:
    mp3_path: str
    srt_path: str
    duration_s: float
    word_count: int


def synthesize(text: str, *, voice: str, out_dir: str | Path, stem: str = "voiceover") -> Voiceover:
    """Synthesize ``text`` to ``<out_dir>/<stem>.mp3`` + ``.srt``.

    Returns a :class:`Voiceover`. Raises :class:`VoiceoverError` if edge-tts is
    not installed, produces no audio, or the network call fails.
    """
    text = (text or "").strip()
    if not text:
        raise VoiceoverError("empty narration")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = out_dir / f"{stem}.mp3"
    srt_path = out_dir / f"{stem}.srt"

    try:
        import edge_tts  # noqa: F401 — presence check; used inside the async helper
    except ImportError as exc:
        raise VoiceoverError("edge-tts not installed (pip install edge-tts)") from exc

    try:
        words = asyncio.run(_stream(text, voice, mp3_path))
    except VoiceoverError:
        raise
    except Exception as exc:  # noqa: BLE001 — transport / event-loop / runtime
        raise VoiceoverError(f"edge-tts synthesis failed: {type(exc).__name__}: {exc}") from exc

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise VoiceoverError("edge-tts produced no audio")

    srt, duration_s = _build_srt(words)
    srt_path.write_text(srt, encoding="utf-8")
    return Voiceover(
        mp3_path=str(mp3_path),
        srt_path=str(srt_path),
        duration_s=duration_s,
        word_count=len(words),
    )


async def _stream(text: str, voice: str, mp3_path: Path) -> list[dict]:
    """Stream synthesis, writing audio to ``mp3_path`` and collecting word
    boundary events. Uses the async ``Communicate.stream()`` available across
    edge-tts versions."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    words: list[dict] = []
    with mp3_path.open("wb") as audio:
        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio" and chunk.get("data"):
                audio.write(chunk["data"])
            elif ctype == "WordBoundary":
                words.append(chunk)
    return words


def _build_srt(words: list[dict]) -> tuple[str, float]:
    """Group word-boundary events into ~7-word captions and render SRT.

    Returns ``(srt_text, last_caption_end_seconds)``. If there are no word
    boundaries (some voices omit them), returns an empty SRT and 0.0 — the
    compositor then renders silent-of-captions but still muxes the audio.
    """
    if not words:
        return "", 0.0

    blocks: list[str] = []
    end_s = 0.0
    index = 1
    for i in range(0, len(words), _WORDS_PER_CAPTION):
        group = words[i : i + _WORDS_PER_CAPTION]
        start_ticks = int(group[0].get("offset", 0))
        last = group[-1]
        end_ticks = int(last.get("offset", 0)) + int(last.get("duration", 0))
        text = " ".join(str(w.get("text", "")).strip() for w in group).strip()
        if not text:
            continue
        start_s = start_ticks / _TICKS_PER_SECOND
        end_s = end_ticks / _TICKS_PER_SECOND
        blocks.append(f"{index}\n{_ts(start_s)} --> {_ts(end_s)}\n{text}\n")
        index += 1
    return "\n".join(blocks), round(end_s, 3)


def _ts(seconds: float) -> str:
    """Format seconds as an SRT timestamp ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000))
    hrs, millis = divmod(millis, 3_600_000)
    mins, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


__all__ = ["Voiceover", "VoiceoverError", "synthesize"]
