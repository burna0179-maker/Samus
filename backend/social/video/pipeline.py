"""Reel pipeline orchestrator — script -> voiceover -> footage -> compose.

Dormant-by-construction and fail-closed: with ``social_reel_enabled`` off it
returns a ``disabled`` result and spends nothing; any stage failure (missing
edge-tts/moviepy/ffmpeg, no Gemini key, budget deny, render error) returns a
populated :class:`ReelResult` with the reason — :func:`produce_reel` never
raises. The finished MP4 + SRT are written under the writable state root
(``<state>/social/reels/<slug>/``).

This produces an asset; it does NOT post. Dispatching a reel to a live network
remains the deliberate, DRY-RUN-by-default job of
:mod:`backend.social.adapters` (and needs a public ``video_url`` first).
"""

from __future__ import annotations

import logging
import re

from backend.common.state_paths import state_path
from backend.social.models import BlogInput
from backend.social.video import footage as _footage
from backend.social.video import script as _script
from backend.social.video import voiceover as _voiceover
from backend.social.video.models import ReelResult, ReelScript

_LOG = logging.getLogger("samus.social.video.pipeline")


def produce_reel(
    blog: BlogInput,
    *,
    settings=None,
    use_llm: bool = True,
    video_approved: bool = False,
    reel_script: ReelScript | None = None,
) -> ReelResult:
    """Produce a social reel MP4 from ``blog`` (or a pre-built ``reel_script``).

    Returns a :class:`ReelResult`. Never raises.
    """
    if settings is None:
        from backend.common.config import get_settings

        settings = get_settings()

    if not bool(getattr(settings, "social_reel_enabled", False)):
        return ReelResult(
            ok=False,
            status="disabled",
            error="social_reel_enabled is off (set SAMUS_SOCIAL_REEL_ENABLED=true)",
        )

    aspect = str(getattr(settings, "social_reel_aspect", "9:16") or "9:16")
    max_segments = int(getattr(settings, "social_reel_max_segments", 5) or 5)
    footage_mode = str(getattr(settings, "social_reel_footage_mode", "image") or "image")
    voice = str(
        getattr(settings, "social_reel_tts_voice", "en-US-AriaNeural") or "en-US-AriaNeural"
    )

    # 1. Script ------------------------------------------------------------
    script = reel_script or _script.build_reel_script(
        blog,
        max_segments=max_segments,
        use_llm=use_llm,
        is_video=(footage_mode == "video"),
        aspect=aspect,
    )
    result = ReelResult(
        ok=False,
        status="error",
        title=script.title,
        aspect=aspect,
        segments_planned=len(script.segments),
        used_llm=script.used_llm,
    )
    if not script.segments:
        result.error = "script produced no segments"
        return result

    out_dir = state_path("social", "reels", _slug(script.title))
    out_dir.mkdir(parents=True, exist_ok=True)

    # 2. Voiceover (+ subtitles) ------------------------------------------
    try:
        vo = _voiceover.synthesize(script.narration_text, voice=voice, out_dir=out_dir)
    except _voiceover.VoiceoverError as exc:
        result.error = f"voiceover: {exc}"
        return result

    # 3. Footage (budget-metered) -----------------------------------------
    footage_paths, footage_report = _footage.generate_segment_footage(
        script.segments,
        settings=settings,
        out_dir=out_dir,
        video_approved=video_approved,
    )
    result.spent_usd = float(footage_report.get("spent_usd", 0.0))
    result.segments_generated = len(footage_paths)
    result.skipped = list(footage_report.get("skipped", []))
    if not footage_paths:
        result.error = f"footage: none generated ({footage_report.get('status')})"
        return result

    # 4. Compose -----------------------------------------------------------
    from backend.social.video import compose as _compose

    music_path = _compose.resolve_music_path(
        str(getattr(settings, "social_reel_music_dir", "") or "")
    )
    mp4_path = out_dir / "reel.mp4"
    try:
        _compose.compose_reel(
            footage_paths,
            vo.mp3_path,
            vo.srt_path,
            out_path=mp4_path,
            aspect=aspect,
            music_path=music_path,
        )
    except _compose.ComposeError as exc:
        result.error = f"compose: {exc}"
        return result

    result.ok = True
    result.status = "ok"
    result.mp4_path = str(mp4_path)
    result.srt_path = vo.srt_path
    result.duration_s = vo.duration_s
    _LOG.info(
        "produced reel '%s' -> %s (%.2fs, $%.4f)",
        script.title,
        mp4_path,
        result.duration_s,
        result.spent_usd,
    )
    return result


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(title: str) -> str:
    s = _SLUG_RE.sub("-", (title or "reel").lower()).strip("-")
    return (s or "reel")[:60]


__all__ = ["produce_reel"]
