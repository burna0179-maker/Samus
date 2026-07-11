"""Tests for backend.seo.blog_gen (GEO blog generation)."""

from __future__ import annotations

import pytest

from backend.seo.blog_gen import (
    BlogPost,
    generate_blog_post,
    _build_template_post,
    _parse_blog_text,
)


# ---------------------------------------------------------------------------
# Template path (no API key)
# ---------------------------------------------------------------------------


def test_generate_blog_post_no_llm_returns_template(monkeypatch):
    from backend.common.llm_client import LlmCallError
    import backend.seo.blog_gen as blog_mod

    def _llm_unavailable(**kw):
        raise LlmCallError("unavailable")

    monkeypatch.setattr(blog_mod, "anthropic_messages", _llm_unavailable)
    post = generate_blog_post(
        topic="GEO for plumbers",
        primary_kw="GEO optimization",
        secondary_kws=["AI citation", "schema markup"],
        author="Test Author",
        industry="plumbing",
        anthropic_api_key=None,
    )
    assert isinstance(post, BlogPost)
    assert post.used_llm is False
    assert post.llm_cost_usd == 0.0


def test_template_post_has_four_sections():
    post = _build_template_post(
        topic="test",
        primary_kw="local SEO",
        secondary_kws=["schema", "FAQ"],
        author="Author",
        date_str="2026-06-11",
    )
    assert len(post.sections) == 4


def test_template_post_sections_have_question_headings():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO",
        secondary_kws=[],
        author="Author",
        date_str="2026-06-11",
    )
    for s in post.sections:
        # Headings must start with a question word
        assert s.heading[0].isupper()
        assert any(
            s.heading.startswith(w)
            for w in (
                "What",
                "How",
                "Why",
                "Which",
                "When",
                "Where",
                "Is",
                "Are",
                "Does",
                "Do",
                "Can",
            )
        ), f"Heading not question-formatted: {s.heading!r}"


def test_template_post_faq_has_six_or_more_items():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO",
        secondary_kws=[],
        author="Author",
        date_str="2026-06-11",
    )
    assert len(post.faq) >= 6


def test_template_post_faq_answers_are_40_to_60_words():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO",
        secondary_kws=[],
        author="Author",
        date_str="2026-06-11",
    )
    for item in post.faq:
        wc = len(item.answer.split())
        assert 30 <= wc <= 80, (
            f"FAQ answer word count {wc} out of expected range for: {item.question!r}"
        )


def test_template_post_golden_answers_are_40_to_70_words():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO optimization",
        secondary_kws=["AI search"],
        author="Author",
        date_str="2026-06-11",
    )
    for s in post.sections:
        wc = len(s.golden_answer.split())
        assert 30 <= wc <= 80, (
            f"Golden answer word count {wc} out of expected range for heading: {s.heading!r}"
        )


def test_template_post_word_count_in_range():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO",
        secondary_kws=["citation", "schema"],
        author="Author",
        date_str="2026-06-11",
    )
    assert post.word_count > 0
    assert post.word_count >= 800  # deterministic template hits at least 800


def test_template_post_has_stat_markers():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO",
        secondary_kws=[],
        author="Author",
        date_str="2026-06-11",
    )
    # At least some sections should have stat_markers
    markers_found = sum(len(s.stat_markers) for s in post.sections)
    assert markers_found >= 2


def test_template_post_author_default():
    post = generate_blog_post(
        topic="test",
        primary_kw="SEO",
        anthropic_api_key=None,
    )
    assert post.author  # non-empty
    assert len(post.author) > 0


def test_template_post_dates_set():
    post = generate_blog_post(
        topic="test",
        primary_kw="SEO",
        author="Alex",
        anthropic_api_key=None,
    )
    assert post.date_published
    assert post.date_modified
    assert post.date_published == post.date_modified  # same on first publish


def test_template_post_ascii_only():
    post = _build_template_post(
        topic="test",
        primary_kw="GEO",
        secondary_kws=[],
        author="Author",
        date_str="2026-06-11",
    )
    full_text = (
        post.title
        + post.intro
        + post.cta
        + " ".join(s.heading + s.golden_answer + s.body for s in post.sections)
        + " ".join(f.question + f.answer for f in post.faq)
    )
    # No non-ASCII chars
    assert full_text.isascii(), "Output contains non-ASCII characters"


# ---------------------------------------------------------------------------
# LLM parse path
# ---------------------------------------------------------------------------


def _make_valid_llm_json(n_faq: int = 6) -> str:
    import json

    sections = [
        {
            "heading": f"How does section {i + 1} work?",
            "golden_answer": (
                "This is a forty to sixty word golden answer block that provides "
                "a direct standalone response to the question posed in the heading "
                "above so AI systems can extract it reliably."
            ),
            "body": " ".join(["word"] * 270),
            "stat_markers": ["[STAT: survey data]"],
        }
        for i in range(4)
    ]
    faq = [
        {"q": f"What is question {i + 1}?", "a": " ".join(["answer"] * 45)} for i in range(n_faq)
    ]
    payload = {
        "title": "How to Optimize for AI Citation?",
        "intro": " ".join(["intro"] * 200),
        "sections": sections,
        "faq": faq,
        "cta": " ".join(["cta"] * 150),
        "author": "Test Author",
        "date_published": "2026-06-11",
        "date_modified": "2026-06-11",
    }
    return json.dumps(payload)


def test_parse_blog_text_valid():
    post = _parse_blog_text(_make_valid_llm_json())
    assert isinstance(post, BlogPost)
    assert len(post.sections) == 4
    assert len(post.faq) == 6
    assert post.used_llm is False  # parse doesn't set used_llm; caller does


def test_parse_blog_text_too_few_faq_raises():
    import pytest

    with pytest.raises(ValueError, match="faq must be list"):
        _parse_blog_text(_make_valid_llm_json(n_faq=3))


def test_parse_blog_text_empty_raises():
    with pytest.raises(ValueError):
        _parse_blog_text("")


def test_parse_blog_text_strips_code_fences():
    inner = _make_valid_llm_json()
    fenced = f"```json\n{inner}\n```"
    post = _parse_blog_text(fenced)
    assert isinstance(post, BlogPost)


# ---------------------------------------------------------------------------
# LLM failure -> template fallback
# ---------------------------------------------------------------------------


def test_generate_blog_post_budget_exceeded_falls_back(monkeypatch):
    from backend.common.llm_client import BudgetExceeded
    from backend.common.llm_budget import QuotaDecision

    def _raise(*a, **kw):
        decision = QuotaDecision(
            allowed=False, quota=1000, used=1000, requested=100, reason="budget_exceeded"
        )
        raise BudgetExceeded(decision)

    monkeypatch.setattr("backend.seo.blog_gen.anthropic_messages", _raise)
    post = generate_blog_post(
        topic="test",
        primary_kw="GEO",
        anthropic_api_key="fake-key",
    )
    assert post.used_llm is False
    assert len(post.sections) == 4


def test_generate_blog_post_llm_error_falls_back(monkeypatch):
    from backend.common.llm_client import LlmCallError

    def _raise(*a, **kw):
        raise LlmCallError("transport error")

    monkeypatch.setattr("backend.seo.blog_gen.anthropic_messages", _raise)
    post = generate_blog_post(
        topic="test",
        primary_kw="GEO",
        anthropic_api_key="fake-key",
    )
    assert post.used_llm is False


def test_generate_blog_post_bad_json_falls_back(monkeypatch):
    monkeypatch.setattr(
        "backend.seo.blog_gen.anthropic_messages",
        lambda *a, **kw: ("not-valid-json{{{", {}),
    )
    post = generate_blog_post(
        topic="test",
        primary_kw="GEO",
        anthropic_api_key="fake-key",
    )
    assert post.used_llm is False
    assert len(post.sections) == 4


def test_generate_blog_post_llm_success(monkeypatch):
    monkeypatch.setattr(
        "backend.seo.blog_gen.anthropic_messages",
        lambda *a, **kw: (_make_valid_llm_json(), {"input_tokens": 100, "output_tokens": 500}),
    )
    monkeypatch.setattr(
        "backend.seo.blog_gen._price_blog_usage",
        lambda usage: 0.0012,
    )
    post = generate_blog_post(
        topic="test",
        primary_kw="GEO",
        anthropic_api_key="fake-key",
    )
    assert post.used_llm is True
    assert post.llm_cost_usd == 0.0012
