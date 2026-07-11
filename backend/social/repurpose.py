"""Blog -> social repurposing.

Every blog post should yield at least eight native social assets (the Opinly
repurposing workflow extended to Facebook): a 2-week distribution package
across LinkedIn, Instagram, X, and Facebook. This multiplies output without
multiplying effort.

Generation is LLM-assisted through the budget-gated ``anthropic_messages``
path, with a **deterministic per-format template fallback** whenever the LLM
is unavailable, over budget, or returns unparseable output — so a repurposing
run *always* produces a complete package, never an exception (mirrors
``outreach.social_adapter.compose_post_via_llm``).

The cardinal rule is "translate the idea, not the text": each format is a
native rewrite, never a copy-paste of the blog excerpt, because cross-posted
prose dies in every algorithm.
"""
from __future__ import annotations

import json
import logging
import os
import re

from backend.social.models import (
    BlogInput,
    PipelineFunction,
    Platform,
    RepurposedAsset,
    RepurposePackage,
    SocialFormat,
)

_LOG = logging.getLogger("samus.social.repurpose")

# The six canonical assets produced from one blog post, with the platform and
# pipeline function each one serves.
_ASSET_PLAN: tuple[tuple[SocialFormat, Platform, PipelineFunction, str], ...] = (
    ("li_text", "linkedin", "educate", "Main argument distilled to 150-250 words; hook-first, no link in body."),
    ("li_carousel", "linkedin", "educate", "Key framework/steps as a 8-10 slide carousel outline; cover slide must stop the scroll."),
    ("li_link", "linkedin", "convert", "Standalone teaser summary; the post says 'link in comments'."),
    ("ig_carousel", "instagram", "educate", "Same framework redesigned for a visual feed carousel (slide-by-slide)."),
    ("ig_reel", "instagram", "engage", "60-90s reel script for one specific tactic; hook on screen in first 2s."),
    ("x_thread", "x", "educate", "5-10 tweet thread expanding the post's core argument; each tweet stands alone."),
    ("fb_post", "facebook", "educate", "Community-style explainer (200-400 words); conversational, question at end to invite discussion."),
    ("fb_link", "facebook", "convert", "Short teaser (2-3 sentences) with blog URL; the link preview does the heavy lifting."),
)

_DEFAULT_WORKCELL = "outreach"  # reuse the proven per-workcell budget config
_MAX_TOKENS = 1400


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def repurpose_blog_post(
    blog: BlogInput,
    *,
    stake_sentence: str = "",  # noqa: ARG001 — carried by the caller into PlannedPost, not the asset
    use_llm: bool = True,
    workcell: str = _DEFAULT_WORKCELL,
    brand_voice_prompt: str | None = None,
    allowed_numeric_sources: list[str] | None = None,
) -> RepurposePackage:
    """Distil ``blog`` into the 6-asset social package.

    Tries a single budget-gated LLM call that returns all six assets as JSON;
    falls back, per missing/invalid field, to a deterministic template. The
    result is always a complete :class:`RepurposePackage`.

    When ``brand_voice_prompt`` is provided, it is passed as a cached system
    prompt so every generated asset adopts the brand's voice / do-say /
    don't-say guidance.

    Every LLM-generated body is run through ``scrub_invented_numbers`` before
    being returned: any digit-bearing claim that is not sourced to
    ``allowed_numeric_sources`` (typically the brand brief's proof_points) is
    replaced with a ``[STAT: ...]`` placeholder. Template bodies are also
    scrubbed so a fail-closed template can never ship an invented number.
    """
    allowed = list(allowed_numeric_sources or [])
    llm_bodies: dict[str, str] = {}
    used_llm = False
    if use_llm:
        llm_bodies = _generate_via_llm(
            blog, workcell=workcell, brand_voice_prompt=brand_voice_prompt,
        )
        used_llm = bool(llm_bodies)

    # Import lazily to keep social→marketing layering optional.
    try:
        from backend.marketing.brand_brief import scrub_invented_numbers
    except Exception:  # noqa: BLE001
        scrub_invented_numbers = None  # type: ignore[assignment]

    assets: list[RepurposedAsset] = []
    for fmt, platform, pipeline_fn, brief_text in _ASSET_PLAN:
        body = (llm_bodies.get(fmt) or "").strip()
        asset_used_llm = bool(body)
        if not body:
            body = _template_for(fmt, blog)
        if scrub_invented_numbers is not None:
            body, flagged = scrub_invented_numbers(body, allowed_sources=allowed)
            if flagged:
                _LOG.info(
                    "repurpose scrubbed %d invented numeric claim(s) in %s: %r",
                    len(flagged), fmt, flagged[:6],
                )
        assets.append(
            RepurposedAsset(
                fmt=fmt,
                platform=platform,
                body=body,
                pipeline_fn=pipeline_fn,
                used_llm=asset_used_llm,
                notes=_notes_for(fmt),
            )
        )

    return RepurposePackage(
        source_title=blog.title,
        source_url=blog.url,
        assets=assets,
        used_llm=used_llm,
    )


# ---------------------------------------------------------------------------
# LLM generation (single structured call; fail-closed)
# ---------------------------------------------------------------------------


def _generate_via_llm(
    blog: BlogInput,
    *,
    workcell: str,
    brand_voice_prompt: str | None = None,
) -> dict[str, str]:
    """Return a ``{format: body}`` map from one LLM call, or ``{}`` on any
    failure (budget, transport, parse). Never raises."""
    try:
        from backend.common.llm_client import anthropic_messages

        keys = ", ".join(fmt for fmt, *_ in _ASSET_PLAN)
        points = "\n".join(f"- {p}" for p in blog.key_points) or "- (none provided)"
        prompt = (
            "You repurpose one blog post into native social assets. Return ONLY a "
            "JSON object whose keys are EXACTLY these format ids: "
            f"{keys}.\n"
            "Each value is the ready-to-post content for that format, written "
            "natively for its platform (never a copy-paste of the blog). "
            "Open with the answer. Be specific via mechanism + outcome, NOT via "
            "invented numbers - any digit-bearing claim that is not in the brand "
            "voice context will be stripped post-generation.\n\n"
            f"BLOG TITLE: {blog.title}\n"
            f"URL: {blog.url or '(none)'}\n"
            f"SUMMARY: {blog.summary or blog.title}\n"
            f"KEY POINTS:\n{points}\n\n"
            "Format guidance: li_text = hook + 3-5 bullet insights + soft CTA "
            "(no link in body). li_carousel / ig_carousel = numbered slide list "
            "(Slide 1 cover ... last slide CTA). li_link = a standalone teaser, "
            "ending 'Full breakdown - link in comments'. ig_reel = a timestamped "
            "60-90s script. x_thread = numbered tweets 1..N. fb_post = "
            "conversational community post (200-400 words), ends with a question. "
            "fb_link = 2-3 sentence teaser with the blog URL inline."
        )
        system = (brand_voice_prompt or "").strip() or None
        text, _usage = anthropic_messages(
            workcell=workcell,
            api_key="unused",
            prompt=prompt,
            system=system,
            cache_system=bool(system),
            max_tokens=_MAX_TOKENS,
            security_label="social_repurpose",
        )
        parsed = _tolerant_json(text)
        if not isinstance(parsed, dict):
            return {}
        # Keep only known format keys with non-empty string bodies.
        valid = {fmt for fmt, *_ in _ASSET_PLAN}
        out: dict[str, str] = {}
        for k, v in parsed.items():
            if k in valid and isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out
    except Exception as exc:  # noqa: BLE001 — fail-closed to templates
        _LOG.info(
            "repurpose llm unavailable (%s), using templates", type(exc).__name__
        )
        return {}


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _tolerant_json(text: str) -> object:
    """Parse a JSON object out of an LLM response that may wrap it in prose or
    a ```json fence. Returns the parsed object or ``None``."""
    text = (text or "").strip()
    if not text:
        return None
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Deterministic templates (zero-cost fallback, one per format)
# ---------------------------------------------------------------------------


def _first_points(blog: BlogInput, n: int) -> list[str]:
    pts = [p.strip() for p in blog.key_points if p.strip()]
    if pts:
        return pts[:n]
    # Derive thin placeholders from the title so a package is never empty.
    return [f"Why it matters for {blog.cluster or 'your pipeline'}", "What most teams get wrong", "The one change that moves the needle"][:n]


def _template_for(fmt: SocialFormat, blog: BlogInput) -> str:
    title = blog.title.strip()
    summary = (blog.summary or title).strip()
    url = blog.url.strip()
    pts = _first_points(blog, 5)

    if fmt == "li_text":
        bullets = "\n".join(f"• {p}" for p in pts)
        return (
            f"Most teams get {title.lower()} wrong.\n\n"
            f"{summary}\n\n"
            f"{bullets}\n\n"
            "What's your experience? 👇"
        )
    if fmt in ("li_carousel", "ig_carousel"):
        slides = [f"Slide 1 (cover): {title}"]
        slides += [f"Slide {i + 2}: {p}" for i, p in enumerate(pts)]
        slides.append("Final slide (CTA): Save this for later — and try it with Hustleforge.")
        return "\n".join(slides)
    if fmt == "li_link":
        tail = "Full breakdown — link in comments 👇" if url else "Full breakdown in the comments 👇"
        return f"{summary}\n\n{tail}"
    if fmt == "ig_reel":
        steps = pts[:3] or ["Step 1", "Step 2", "Step 3"]
        body = [
            f"[0-3s HOOK] On-screen: \"{title}\"",
            "[3-15s TENSION] Here's the problem with how most teams approach this...",
        ]
        body += [f"[{15 + i * 12}-{27 + i * 12}s] {s}" for i, s in enumerate(steps)]
        body.append("[CTA] Save this — link in bio.")
        return "\n".join(body)
    if fmt == "x_thread":
        tweets = [f"1/ {title}\n\n{summary}\n\nHere's what we learned: 🧵"]
        tweets += [f"{i + 2}/ {p}" for i, p in enumerate(pts)]
        tweets.append(f"{len(pts) + 2}/ If this was useful, repost tweet 1." + (f"\n\n{url}" if url else ""))
        return "\n\n".join(tweets)
    if fmt == "x_tweet":
        return f"{summary}" + (f"\n\n{url}" if url else "")
    if fmt == "ig_story":
        return f"Poll: What's your biggest challenge with {blog.cluster or title.lower()}?"
    if fmt == "fb_post":
        bullets = "\n".join(f"• {p}" for p in pts)
        return (
            f"Let's talk about {title.lower()}.\n\n"
            f"{summary}\n\n"
            f"Here's what we've seen work:\n{bullets}\n\n"
            f"What's been your experience? Drop a comment below 👇"
        )
    if fmt == "fb_link":
        tail = f"\n\n🔗 {url}" if url else ""
        return f"{summary}{tail}"
    return summary


_NOTES: dict[str, str] = {
    "li_text": "Reply to early comments within 60 min; don't start with 'I'.",
    "li_carousel": "Consistent brand colors; cover slide decides reach.",
    "li_link": "Put the link in the FIRST COMMENT, not the body.",
    "ig_carousel": "Carousels get the most saves — design for thumbnail legibility.",
    "ig_reel": "Captions mandatory; vertical 9:16; trending audio.",
    "x_thread": "Post as a reply chain, not scheduled; each tweet must stand alone.",
    "x_tweet": "Pin your best-performing one.",
    "ig_story": "Existing-audience engagement; not for net-new reach.",
    "fb_post": "Conversational tone; ask a question; reply to comments for algorithm boost.",
    "fb_link": "Include the URL directly in the body — Facebook renders a native link preview.",
}


def _notes_for(fmt: SocialFormat) -> str:
    return _NOTES.get(fmt, "")
