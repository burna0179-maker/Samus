"""Tests for backend.social.video.script — reel script generation.

The LLM backend (local LM Studio, free) fires whenever ``use_llm=True`` --
tests either pass ``use_llm=False`` for the pure deterministic path or stub
the LLM entrypoint (raise / fake response) so nothing here depends on a real
LM Studio being reachable at test time.
"""
from __future__ import annotations

from backend.social.models import BlogInput
from backend.social.video.script import build_reel_script, script_from_ig_reel


def _blog() -> BlogInput:
    return BlogInput(
        title="The 44% rule in AI content",
        url="https://hustleforge.ai/blog/the-44-percent-rule",
        summary="44% of AI citations come from the first 30% of a page.",
        key_points=[
            "Front-load the answer in the first 60 words",
            "Add a 5-question FAQ block",
            "Mark it up with FAQPage schema",
        ],
        cluster="AI visibility",
    )


def test_build_script_templated_has_hook_and_segments():
    script = build_reel_script(_blog(), use_llm=False)
    assert script.hook.strip()
    assert 1 <= len(script.segments) <= 5
    assert script.used_llm is False
    assert script.aspect == "9:16"


def test_build_script_respects_max_segments():
    script = build_reel_script(_blog(), use_llm=False, max_segments=2)
    assert len(script.segments) == 2


def test_build_script_every_segment_has_visual_prompt():
    script = build_reel_script(_blog(), use_llm=False)
    assert all(s.visual_prompt.strip() for s in script.segments)
    # The generic visual prompt must forbid on-screen text.
    assert all("no text" in s.visual_prompt.lower() for s in script.segments)


def test_build_script_minimal_input_never_empty():
    script = build_reel_script(BlogInput(title="Just a title"), use_llm=False)
    assert len(script.segments) >= 1
    assert script.narration_text.strip()


def test_build_script_use_llm_llm_error_falls_back(monkeypatch):
    """With use_llm=True the free local LLM (LM Studio) fires unconditionally.
    When the call errors, the reel is still built from the deterministic
    templated path."""
    import backend.common.llm_client as llm

    def _boom(**kwargs):
        raise RuntimeError("stub_lm_studio_down")

    # script does `from backend.common.llm_client import anthropic_messages`
    # at call time -> patch the source module.
    monkeypatch.setattr(llm, "anthropic_messages", _boom, raising=False)
    script = build_reel_script(_blog(), use_llm=True)
    assert script.used_llm is False
    assert len(script.segments) >= 1


def test_build_script_video_mode_marks_segments():
    script = build_reel_script(_blog(), use_llm=False, is_video=True)
    assert all(s.is_video for s in script.segments)


def test_narration_text_starts_with_hook():
    script = build_reel_script(_blog(), use_llm=False)
    assert script.narration_text.startswith(script.hook.strip())


# --- script_from_ig_reel ----------------------------------------------------


def test_script_from_ig_reel_strips_timecodes():
    body = (
        '[0-3s HOOK] On-screen: "Stop guessing at AI visibility"\n'
        "[3-15s TENSION] Here's the problem with how most teams approach this...\n"
        "[15-27s] Front-load the answer in the first 60 words\n"
        "[CTA] Save this — link in bio."
    )
    script = script_from_ig_reel(body, title="AI visibility reel")
    assert script.hook == "Stop guessing at AI visibility"
    assert len(script.segments) >= 1
    # No leftover timecode brackets in any narration line.
    assert all("[" not in s.narration for s in script.segments)


def test_script_from_ig_reel_empty_body_is_safe():
    script = script_from_ig_reel("", title="Fallback")
    assert len(script.segments) >= 1
    assert script.hook.strip()
