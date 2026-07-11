"""Video composition — stitch footage + voiceover + captions into an MP4.

Uses MoviePy 2.x, imported lazily (it pulls Pillow and needs the ``ffmpeg``
binary at runtime). The module imports without it; only :func:`compose_reel`
requires it and raises :class:`ComposeError` on any failure so the pipeline
fails closed.

Composition mirrors MoneyPrinterTurbo's output: footage is fit (cover + centre
crop) to a 1080x1920 vertical frame, stills get a slow Ken-Burns zoom, the
voiceover drives the total duration, optional royalty-free music is ducked
underneath, and SRT captions are burned in near the lower third. Caption
rendering degrades gracefully (logs + skips) if the font backend is unavailable
rather than failing the whole render.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

_LOG = logging.getLogger("samus.social.video.compose")

_ASPECT_SIZES = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}
_FPS = 30
_MUSIC_VOLUME = 0.12
_KEN_BURNS_ZOOM_PER_S = 0.03  # +3% scale per second


class ComposeError(Exception):
    """Video composition failed (missing moviepy/ffmpeg, or a render error)."""


def compose_reel(
    footage_paths: list[str],
    voiceover_mp3: str,
    srt_path: str,
    *,
    out_path: str | Path,
    aspect: str = "9:16",
    music_path: str = "",
) -> str:
    """Render the reel to ``out_path`` (mp4). Returns the path. Raises ComposeError."""
    if not footage_paths:
        raise ComposeError("no footage to compose")
    if not voiceover_mp3 or not Path(voiceover_mp3).exists():
        raise ComposeError("voiceover audio missing")

    try:
        import moviepy  # noqa: F401
    except ImportError as exc:
        raise ComposeError("moviepy not installed (pip install 'moviepy>=2')") from exc

    try:
        return _render(footage_paths, voiceover_mp3, srt_path, out_path=Path(out_path),
                       aspect=aspect, music_path=music_path)
    except ComposeError:
        raise
    except Exception as exc:  # noqa: BLE001 — ffmpeg / codec / Pillow / IO
        raise ComposeError(f"render failed: {type(exc).__name__}: {exc}") from exc


def _render(footage_paths, voiceover_mp3, srt_path, *, out_path, aspect, music_path) -> str:
    from moviepy import (
        AudioFileClip,
        CompositeAudioClip,
        CompositeVideoClip,
        ImageClip,
        VideoFileClip,
        concatenate_videoclips,
    )

    width, height = _ASPECT_SIZES.get(aspect, _ASPECT_SIZES["9:16"])

    voice = AudioFileClip(voiceover_mp3)
    total = float(voice.duration)
    if total <= 0:
        raise ComposeError("voiceover has zero duration")

    n = len(footage_paths)
    per_shot = total / n
    shots = []
    for i, path in enumerate(footage_paths):
        shots.append(_shot_clip(path, per_shot, width, height,
                                ImageClip=ImageClip, VideoFileClip=VideoFileClip))
    video = concatenate_videoclips(shots, method="compose").with_duration(total)

    layers = [video]
    layers.extend(_caption_clips(srt_path, width, height, total))
    composite = CompositeVideoClip(layers, size=(width, height))

    audio = voice
    music = _music_track(music_path, total, AudioFileClip=AudioFileClip)
    if music is not None:
        audio = CompositeAudioClip([voice, music])
    composite = composite.with_audio(audio).with_duration(total)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    composite.write_videofile(
        str(out_path), fps=_FPS, codec="libx264", audio_codec="aac",
        preset="medium", logger=None,
    )
    composite.close()
    voice.close()
    return str(out_path)


def _shot_clip(path: str, duration: float, width: int, height: int, *, ImageClip, VideoFileClip):
    """Build one shot fit to the target frame for ``duration`` seconds."""
    p = str(path).lower()
    if p.endswith((".mp4", ".mov", ".webm", ".mkv")):
        clip = VideoFileClip(path)
        if clip.duration and clip.duration < duration:
            clip = clip.with_effects([_loop_effect(duration)])
        clip = clip.subclipped(0, min(duration, clip.duration or duration))
        return _fit(clip, width, height).with_duration(duration).without_audio()
    # Still image -> Ken-Burns slow zoom.
    clip = ImageClip(path).with_duration(duration)
    clip = _fit(clip, width, height)
    return clip.resized(lambda t: 1 + _KEN_BURNS_ZOOM_PER_S * t).with_position("center").with_duration(duration)


def _fit(clip, width: int, height: int):
    """Cover the target frame then centre-crop (no letterboxing)."""
    cw, ch = clip.w, clip.h
    scale = max(width / cw, height / ch)
    clip = clip.resized(scale)
    return clip.cropped(width=width, height=height, x_center=clip.w / 2, y_center=clip.h / 2)


def _loop_effect(duration: float):
    """Return a MoviePy 2.x Loop effect covering ``duration`` (best-effort)."""
    from moviepy.video.fx import Loop  # type: ignore

    return Loop(duration=duration)


def _caption_clips(srt_path: str, width: int, height: int, total: float) -> list:
    """Build burned-in caption TextClips from an SRT. Degrades to [] on any
    font/render issue so a missing font backend never kills the reel."""
    if not srt_path or not Path(srt_path).exists():
        return []
    try:
        from moviepy import TextClip
    except ImportError:
        return []

    cues = _parse_srt(Path(srt_path).read_text(encoding="utf-8"))
    if not cues:
        return []

    clips = []
    box_w = int(width * 0.86)
    font_size = int(height * 0.045)
    for start, end, text in cues:
        if start >= total:
            break
        dur = max(0.1, min(end, total) - start)
        try:
            tc = (
                TextClip(
                    text=text, font_size=font_size, color="white", method="caption",
                    size=(box_w, None), stroke_color="black", stroke_width=2,
                    text_align="center",
                )
                .with_start(start)
                .with_duration(dur)
                .with_position(("center", int(height * 0.72)))
            )
            clips.append(tc)
        except Exception as exc:  # noqa: BLE001 — font backend unavailable
            _LOG.info("caption render unavailable (%s); reel will have no burned captions", type(exc).__name__)
            return []
    return clips


_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _parse_srt(text: str) -> list[tuple[float, float, str]]:
    """Parse SRT into ``(start_s, end_s, text)`` cues. Tolerant of blank lines."""
    cues: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        m = None
        body_start = 0
        for i, ln in enumerate(lines):
            m = _SRT_TIME_RE.search(ln)
            if m:
                body_start = i + 1
                break
        if not m:
            continue
        start = _to_s(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _to_s(m.group(5), m.group(6), m.group(7), m.group(8))
        body = " ".join(lines[body_start:]).strip()
        if body:
            cues.append((start, end, body))
    return cues


def _to_s(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _music_track(music_path: str, duration: float, *, AudioFileClip):
    """Load + duck a background track to ``duration``, or None if unavailable."""
    if not music_path:
        return None
    try:
        track = AudioFileClip(music_path)
        end = min(track.duration or duration, duration)
        return track.subclipped(0, end).with_volume_scaled(_MUSIC_VOLUME)
    except Exception as exc:  # noqa: BLE001 — bad/missing audio file
        _LOG.info("background music unavailable (%s); skipping", type(exc).__name__)
        return None


def resolve_music_path(music_dir: str) -> str:
    """Pick a deterministic (first, sorted) audio file from ``music_dir``, or ''."""
    if not music_dir:
        return ""
    d = Path(music_dir)
    if not d.is_dir():
        return ""
    tracks = sorted(p for p in d.iterdir() if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac", ".ogg"))
    return str(tracks[0]) if tracks else ""


__all__ = ["ComposeError", "compose_reel", "resolve_music_path"]
