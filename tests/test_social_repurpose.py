"""Tests for backend.social.repurpose — blog -> 8-asset social package.

The LLM backend (local LM Studio, free) fires whenever ``use_llm=True`` --
tests either pass ``use_llm=False`` for the pure deterministic path or stub
the LLM entrypoint (raise / fake response) so nothing here depends on a real
LM Studio being reachable at test time.
"""

from __future__ import annotations

from backend.social.models import BlogInput
from backend.social.repurpose import (
    _ASSET_PLAN,
    _tolerant_json,
    repurpose_blog_post,
)


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


def test_repurpose_produces_eight_assets():
    pkg = repurpose_blog_post(_blog(), use_llm=False)
    assert len(pkg.assets) == 8
    assert pkg.source_title == "The 44% rule in AI content"
    assert pkg.used_llm is False


def test_repurpose_formats_match_asset_plan():
    pkg = repurpose_blog_post(_blog(), use_llm=False)
    got = [(a.fmt, a.platform, a.pipeline_fn) for a in pkg.assets]
    expected = [(fmt, plat, fn) for fmt, plat, fn, _ in _ASSET_PLAN]
    assert got == expected


def test_repurpose_templated_assets_marked_not_llm():
    pkg = repurpose_blog_post(_blog(), use_llm=False)
    assert all(a.used_llm is False for a in pkg.assets)
    assert all(a.body.strip() for a in pkg.assets)


def test_repurpose_carousel_has_cover_and_cta():
    pkg = repurpose_blog_post(_blog(), use_llm=False)
    carousel = next(a for a in pkg.assets if a.fmt == "li_carousel")
    assert "Slide 1 (cover)" in carousel.body
    assert "CTA" in carousel.body


def test_repurpose_link_post_keeps_link_out_of_body():
    pkg = repurpose_blog_post(_blog(), use_llm=False)
    link = next(a for a in pkg.assets if a.fmt == "li_link")
    # The URL must NOT be inlined — LinkedIn punishes links in the body.
    assert "https://" not in link.body
    assert "comments" in link.body.lower()
    assert "first comment" in link.notes.lower()


def test_repurpose_thread_is_numbered():
    pkg = repurpose_blog_post(_blog(), use_llm=False)
    thread = next(a for a in pkg.assets if a.fmt == "x_thread")
    assert thread.body.startswith("1/")


def test_repurpose_never_empty_with_minimal_input():
    pkg = repurpose_blog_post(BlogInput(title="Just a title"), use_llm=False)
    assert len(pkg.assets) == 8
    assert all(a.body.strip() for a in pkg.assets)


def test_repurpose_use_llm_true_llm_error_falls_back(monkeypatch):
    """With use_llm=True the free local LLM (LM Studio) fires unconditionally.
    When the call errors, the eight assets still get generated from the
    deterministic templates so the package is never short."""
    import backend.common.llm_client as llm

    def _boom(**kwargs):
        raise RuntimeError("stub_lm_studio_down")

    # repurpose does `from backend.common.llm_client import anthropic_messages`
    # at call time -> patch the source module.
    monkeypatch.setattr(llm, "anthropic_messages", _boom, raising=False)
    pkg = repurpose_blog_post(_blog(), use_llm=True)
    assert pkg.used_llm is False
    assert len(pkg.assets) == 8


# --- tolerant JSON parser ---------------------------------------------------


def test_tolerant_json_plain_object():
    assert _tolerant_json('{"li_text": "hi"}') == {"li_text": "hi"}


def test_tolerant_json_fenced():
    raw = 'Sure!\n```json\n{"x_thread": "1/ ..."}\n```\nDone.'
    assert _tolerant_json(raw) == {"x_thread": "1/ ..."}


def test_tolerant_json_prose_wrapped():
    raw = 'Here you go: {"li_text": "body"} hope that helps'
    assert _tolerant_json(raw) == {"li_text": "body"}


def test_tolerant_json_garbage_returns_none():
    assert _tolerant_json("not json at all") is None
    assert _tolerant_json("") is None
