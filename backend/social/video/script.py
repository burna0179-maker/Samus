"""Reel script generation — blog/brief -> a hook + per-shot narration script.

Mirrors :mod:`backend.social.repurpose`: one budget-gated LLM call returns a
structured script as JSON; whenever the LLM is unavailable, over budget, or
returns unparseable output we fall back, field by field, to a deterministic
template so a script is *always* produced (never an exception). The tolerant
JSON parser and the key-point fallback are reused from ``repurpose`` so the two
generators behave identically.

A reel script is the **content** layer only — turning it into a video (voiceover
+ footage + composition) is the job of the rest of the package.
"""

from __future__ import annotations

import logging

from backend.social.models import BlogInput
from backend.social.repurpose import _first_points, _tolerant_json
from backend.social.video.models import ReelScript, ReelSegment

_LOG = logging.getLogger("samus.social.video.script")

_DEFAULT_WORKCELL = "outreach"  # reuse the proven per-workcell budget config
_MAX_TOKENS = 900
_DEFAULT_SECONDS_PER_SHOT = 5.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_reel_script(
    blog: BlogInput,
    *,
    max_segments: int = 5,
    use_llm: bool = True,
    is_video: bool = False,
    aspect: str = "9:16",
    workcell: str = _DEFAULT_WORKCELL,
) -> ReelScript:
    """Distil ``blog`` into a reel script of at most ``max_segments`` shots.

    Tries a single budget-gated LLM call; falls back to a deterministic template
    derived from the blog's key points. Always returns a complete
    :class:`ReelScript` with >= 1 segment.
    """
    max_segments = max(1, int(max_segments))
    parsed = (
        _generate_via_llm(blog, max_segments=max_segments, workcell=workcell) if use_llm else None
    )

    hook = ""
    raw_segments: list[tuple[str, str]] = []
    used_llm = False
    if parsed:
        hook = str(parsed.get("hook") or "").strip()
        for item in parsed.get("segments") or []:
            if not isinstance(item, dict):
                continue
            narration = str(item.get("narration") or item.get("text") or "").strip()
            visual = str(item.get("visual") or item.get("visual_prompt") or "").strip()
            if narration:
                raw_segments.append((narration, visual))
        raw_segments = raw_segments[:max_segments]
        used_llm = bool(raw_segments)

    if not raw_segments:
        hook = hook or _template_hook(blog)
        raw_segments = _template_segments(blog, max_segments)

    segments = [
        ReelSegment(
            narration=narration,
            visual_prompt=visual or _visual_from_narration(blog, narration),
            seconds=_DEFAULT_SECONDS_PER_SHOT,
            is_video=is_video,
        )
        for narration, visual in raw_segments
    ]
    return ReelScript(
        title=blog.title.strip() or "Untitled reel",
        hook=hook or _template_hook(blog),
        segments=segments,
        aspect=aspect,
        used_llm=used_llm,
    )


def script_from_ig_reel(
    asset_body: str,
    *,
    title: str = "",
    is_video: bool = False,
    aspect: str = "9:16",
    max_segments: int = 5,
) -> ReelScript:
    """Build a :class:`ReelScript` from an existing repurposed ``ig_reel`` body.

    ``repurpose`` emits ``ig_reel`` bodies as timestamped lines such as
    ``[0-3s HOOK] On-screen: "..."`` / ``[15-27s] <tactic>``. We strip the
    timestamp/label prefix and treat the first line as the hook and the rest as
    shots. Always returns >= 1 segment; never raises.
    """
    lines = [ln.strip() for ln in (asset_body or "").splitlines() if ln.strip()]
    cleaned = [_strip_timecode(ln) for ln in lines]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        cleaned = [title.strip() or "Here's the one thing most teams miss."]

    hook = cleaned[0]
    body = cleaned[1:][: max(1, int(max_segments))] or [hook]
    segments = [
        ReelSegment(
            narration=line,
            visual_prompt=_visual_from_narration(BlogInput(title=title or hook), line),
            seconds=_DEFAULT_SECONDS_PER_SHOT,
            is_video=is_video,
        )
        for line in body
    ]
    return ReelScript(
        title=title.strip() or hook,
        hook=hook,
        segments=segments,
        aspect=aspect,
        used_llm=False,
    )


# ---------------------------------------------------------------------------
# LLM generation (single structured call; fail-closed)
# ---------------------------------------------------------------------------


def _generate_via_llm(blog: BlogInput, *, max_segments: int, workcell: str) -> dict | None:
    """Return the parsed script dict from one LLM call, or ``None`` on any
    failure (no key, budget, transport, parse). Never raises."""
    try:
        from backend.common.llm_client import anthropic_messages

        points = "\n".join(f"- {p}" for p in blog.key_points) or "- (none provided)"
        prompt = (
            "You script a short vertical social video (a Reel/Short). Return ONLY a "
            "JSON object with keys 'hook' (a <=12-word on-screen opener that stops the "
            f"scroll in the first 2 seconds) and 'segments' (a list of {max_segments} "
            "items). Each segment has 'narration' (ONE spoken sentence, ~12-22 words, "
            "conversational, specific, no hashtags) and 'visual' (a short photographic "
            "description of the b-roll shot for that line — concrete subject + setting, "
            "no text/words/logos).\n\n"
            f"TOPIC: {blog.title}\n"
            f"SUMMARY: {blog.summary or blog.title}\n"
            f"KEY POINTS:\n{points}\n\n"
            "The narration must read as one flowing voiceover when concatenated. "
            "Open with the answer; use a real number where you can."
        )
        text, _usage = anthropic_messages(
            workcell=workcell,
            api_key="unused",
            prompt=prompt,
            max_tokens=_MAX_TOKENS,
        )
        parsed = _tolerant_json(text)
        if isinstance(parsed, dict) and parsed.get("segments"):
            return parsed
        return None
    except Exception as exc:  # noqa: BLE001 — fail-closed to templates
        _LOG.info("reel script llm unavailable (%s), using templates", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Deterministic templates (zero-cost fallback)
# ---------------------------------------------------------------------------


def _template_hook(blog: BlogInput) -> str:
    title = blog.title.strip() or (blog.cluster or "your pipeline")
    return f"Most teams get {title.lower()} wrong."


def _template_segments(blog: BlogInput, max_segments: int) -> list[tuple[str, str]]:
    pts = _first_points(blog, max_segments)
    segs: list[tuple[str, str]] = []
    for p in pts:
        narration = p.strip().rstrip(".") + "."
        segs.append((narration, _visual_from_narration(blog, narration)))
    if not segs:
        narration = (
            blog.summary or blog.title or "Here's the one change that moves the needle."
        ).strip()
        segs.append((narration, _visual_from_narration(blog, narration)))
    return segs[:max_segments]


def _visual_from_narration(blog: BlogInput, narration: str) -> str:
    """A safe, generic photographic b-roll prompt when the LLM gave none.

    Concrete enough for Gemini, deliberately people-light so the people
    directive (added downstream by media_gen) governs any human depiction.
    """
    topic = (blog.cluster or blog.title or "modern business").strip()
    snippet = " ".join(narration.split())[:80]
    return (
        f"Cinematic vertical b-roll illustrating: {snippet}. "
        f"Theme: {topic}. Photorealistic, shallow depth of field, natural light. "
        "No text, no words, no logos, no watermarks."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import re  # noqa: E402 — kept local to the one helper that needs it

_TIMECODE_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_ONSCREEN_RE = re.compile(r"^\s*(?:on-screen|on screen|cta|hook|text)\s*:\s*", re.IGNORECASE)


def _strip_timecode(line: str) -> str:
    """Strip a leading ``[0-3s ...]`` timecode and an ``On-screen:`` label,
    plus surrounding quotes, from a templated reel-script line."""
    out = _TIMECODE_RE.sub("", line)
    out = _ONSCREEN_RE.sub("", out)
    return out.strip().strip('"').strip()


__all__ = ["build_reel_script", "script_from_ig_reel"]
