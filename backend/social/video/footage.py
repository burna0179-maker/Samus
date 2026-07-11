"""Per-shot AI footage for a reel — reuses the website media generator.

We do NOT add a second image/video code path: this module calls the existing,
verified :mod:`backend.website.media_gen` primitives (``generate_image`` for
Gemini stills, ``start_video``/``poll_video``/``download_video`` for Veo) and
meters every paid call through the SAME :class:`backend.common.media_budget.
MediaBudgetStore` the website generator uses — so a reel and a site share one
daily dollar cap.

Cost posture (the cost-sane default the user signed off on): stills are the
default (~$0.04 each); Veo motion clips are produced only when the segment is
flagged ``is_video`` AND ``social_reel_video_enabled`` AND (approval not required
OR ``video_approved``). Every shot forces the reel aspect (9:16 by default).

Fail-closed: a per-shot failure or budget deny is recorded in the report and
skipped; the function never raises. Returns ``(footage_paths, report)``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.social.video.models import ReelSegment

_LOG = logging.getLogger("samus.social.video.footage")


def generate_segment_footage(
    segments: list[ReelSegment],
    *,
    settings: Any,
    out_dir: str | Path,
    video_approved: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """Generate one footage file per segment under the paid-media budget.

    Returns ``(paths, report)`` where ``paths`` is aligned to ``segments`` order
    (only successful shots are included) and ``report`` carries the spend +
    skip detail. Spends nothing when no key is set or the budget denies.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    api_key = (getattr(settings, "gemini_api_key", "") or "").strip()
    if not api_key:
        return [], {
            "status": "no_api_key",
            "generated": 0,
            "skipped": ["all:no_api_key"],
            "spent_usd": 0.0,
        }

    # Lazy: media_gen imports httpx at module load — fine in the Samus image, but
    # keep it local so this module's import surface stays minimal.
    from backend.common.media_budget import MediaBudgetStore
    from backend.website import media_gen

    budget = MediaBudgetStore(float(getattr(settings, "media_daily_dollar_cap", 0.0)))
    aspect = str(getattr(settings, "social_reel_aspect", "9:16") or "9:16")
    footage_mode = str(getattr(settings, "social_reel_footage_mode", "image") or "image")
    video_enabled = bool(getattr(settings, "social_reel_video_enabled", False))
    requires_approval = bool(getattr(settings, "media_video_requires_approval", True))
    img_cost = float(getattr(settings, "media_image_cost_usd", 0.04))
    vid_cost = float(getattr(settings, "media_video_cost_usd", 1.50))
    img_model = str(getattr(settings, "website_media_image_model", "gemini-2.5-flash-image"))
    vid_model = str(getattr(settings, "website_media_video_model", "veo-3.1-fast-generate-preview"))
    vid_res = str(getattr(settings, "website_media_video_resolution", "720p"))
    vid_secs = str(getattr(settings, "website_media_video_seconds", "8"))
    people = (getattr(settings, "website_media_people_directive", "") or "").strip()

    paths: list[str] = []
    skipped: list[str] = []
    spent = 0.0

    for idx, seg in enumerate(segments):
        as_video = bool(seg.is_video) and footage_mode == "video" and video_enabled
        if as_video and requires_approval and not video_approved:
            skipped.append(f"shot{idx}:video_requires_approval")
            as_video = False  # degrade to a still so the reel still renders

        est = vid_cost if as_video else img_cost
        decision = budget.can_spend(est)
        if not decision.allowed:
            skipped.append(f"shot{idx}:{decision.reason}")
            continue

        prompt = _shot_prompt(seg.visual_prompt, people=people)
        try:
            if as_video:
                path = _make_video(
                    media_gen,
                    prompt,
                    idx,
                    out_dir,
                    api_key=api_key,
                    model=vid_model,
                    aspect=aspect,
                    resolution=vid_res,
                    seconds=vid_secs,
                )
            else:
                path = _make_image(
                    media_gen,
                    prompt,
                    idx,
                    out_dir,
                    api_key=api_key,
                    model=img_model,
                    aspect=aspect,
                )
        except media_gen.MediaGenError as exc:
            skipped.append(f"shot{idx}:{type(exc).__name__}:{exc}")
            continue

        budget.record(est)
        spent += est
        paths.append(path)

    report = {
        "status": "ok" if paths else "empty",
        "generated": len(paths),
        "planned": len(segments),
        "skipped": skipped,
        "spent_usd": round(spent, 4),
    }
    return paths, report


def _shot_prompt(visual_prompt: str, *, people: str) -> str:
    base = (
        visual_prompt
        or "Cinematic vertical b-roll. Photorealistic, natural light. "
        "No text, no words, no logos, no watermarks."
    ).strip()
    # Append the representation directive only when the shot may depict people.
    if people and any(
        w in base.lower()
        for w in ("person", "people", "team", "worker", "customer", "hand", "staff", "man", "woman")
    ):
        base = f"{base} {people}"
    return base


def _make_image(
    media_gen, prompt: str, idx: int, out_dir: Path, *, api_key: str, model: str, aspect: str
) -> str:
    data = media_gen.generate_image(prompt, api_key=api_key, model=model, aspect_ratio=aspect)
    path = out_dir / f"shot_{idx:02d}.png"
    path.write_bytes(data)
    return str(path)


def _make_video(
    media_gen,
    prompt: str,
    idx: int,
    out_dir: Path,
    *,
    api_key: str,
    model: str,
    aspect: str,
    resolution: str,
    seconds: str,
) -> str:
    op = media_gen.start_video(
        prompt,
        api_key=api_key,
        model=model,
        aspect_ratio=aspect,
        resolution=resolution,
        seconds=seconds,
    )
    result = media_gen.poll_video(op, api_key=api_key)
    uri = media_gen._extract_video_uri(result)
    if not uri:
        raise media_gen.MediaGenError("no_video_uri_in_result")
    data = media_gen.download_video(uri, api_key=api_key)
    path = out_dir / f"shot_{idx:02d}.mp4"
    path.write_bytes(data)
    return str(path)


__all__ = ["generate_segment_footage"]
