"""Dataclasses for the social reel engine.

Stdlib-only (mirrors :mod:`backend.social.models`) so the script generator,
voiceover, footage, compositor, pipeline, CLI, and tests all share one set of
shapes without dragging in the heavy media dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# How a single shot is rendered. "image" = a Gemini still animated with a
# Ken-Burns pan/zoom (cheap default); "video" = a Veo motion clip (premium).
FootageMode = Literal["image", "video"]


@dataclass
class ReelSegment:
    """One narrated shot of the reel.

    ``narration`` is spoken (and captioned); ``visual_prompt`` drives footage
    generation; ``seconds`` is an estimate used only for planning — the final
    shot length is locked to the segment's slice of the real voiceover.
    """

    narration: str
    visual_prompt: str
    seconds: float = 5.0
    is_video: bool = False  # render this shot as a Veo motion clip vs. a still

    def to_dict(self) -> dict[str, Any]:
        return {
            "narration": self.narration,
            "visual_prompt": self.visual_prompt,
            "seconds": round(self.seconds, 2),
            "is_video": self.is_video,
        }


@dataclass
class ReelScript:
    """A full reel script: a scroll-stopping hook plus ordered shots."""

    title: str
    hook: str = ""
    segments: list[ReelSegment] = field(default_factory=list)
    aspect: str = "9:16"
    used_llm: bool = False

    @property
    def narration_text(self) -> str:
        """The complete spoken script (hook first), one shot per line."""
        lines = [self.hook.strip()] if self.hook.strip() else []
        lines += [s.narration.strip() for s in self.segments if s.narration.strip()]
        return "\n".join(lines)

    @property
    def total_seconds(self) -> float:
        return round(sum(s.seconds for s in self.segments), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "hook": self.hook,
            "aspect": self.aspect,
            "used_llm": self.used_llm,
            "total_seconds": self.total_seconds,
            "segments": [s.to_dict() for s in self.segments],
        }


@dataclass
class ReelResult:
    """Outcome of one :func:`backend.social.video.pipeline.produce_reel` run.

    ``ok`` is True only when an MP4 was written. Every failure mode (disabled,
    missing dep, budget deny, compose error) returns a populated result with a
    reason — the pipeline never raises.
    """

    ok: bool
    status: str = ""  # disabled | no_api_key | ok | error | dry_run
    mp4_path: str = ""
    srt_path: str = ""
    title: str = ""
    aspect: str = "9:16"
    duration_s: float = 0.0
    segments_planned: int = 0
    segments_generated: int = 0
    used_llm: bool = False
    spent_usd: float = 0.0
    skipped: list[str] = field(default_factory=list)
    error: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "mp4_path": self.mp4_path,
            "srt_path": self.srt_path,
            "title": self.title,
            "aspect": self.aspect,
            "duration_s": round(self.duration_s, 2),
            "segments_planned": self.segments_planned,
            "segments_generated": self.segments_generated,
            "used_llm": self.used_llm,
            "spent_usd": round(self.spent_usd, 4),
            "skipped": list(self.skipped),
            "error": self.error,
            "dry_run": self.dry_run,
        }


__all__ = ["FootageMode", "ReelSegment", "ReelScript", "ReelResult"]
