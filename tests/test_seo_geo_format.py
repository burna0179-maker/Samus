"""Tests for backend.seo.geo_format — answer-first + FAQ.

The FAQ LLM path fires the free local backend (LM Studio) whenever
``use_llm=True``. Tests either pass ``use_llm=False`` for the pure
deterministic path or stub the LLM entrypoint (raise) so nothing here
depends on a real LM Studio being reachable at test time.
"""

from __future__ import annotations

from backend.seo.geo_format import (
    GOLDEN_MAX_WORDS,
    build_faq_templated,
    geo_enrich,
    golden_answer_from,
    _tolerant_json_array,
)

_DRAFTS = {
    "title": "Emergency Plumbing | Fast, Transparent Pricing",
    "meta_description": "Need a plumber fast? We arrive same-day with upfront pricing.",
    "h1": "Emergency Plumbing You Can Trust",
    "body_intro": (
        "Emergency plumbing means a burst pipe or a flooded floor can't wait. "
        "Our licensed team arrives the same day, diagnoses the problem on the "
        "spot, and quotes a fixed price before any work starts. No surprises, "
        "no jargon, no overtime games."
    ),
    "body_main": "We handle burst pipes, blocked drains, and water heaters.",
    "cta": "Call now for a free same-day quote.",
}
_KEYWORDS = ["emergency plumbing", "drain cleaning", "water heater repair"]


def test_golden_answer_within_word_budget():
    g = golden_answer_from("What is emergency plumbing?", _DRAFTS["body_intro"])
    n = len(g.split())
    assert n <= GOLDEN_MAX_WORDS
    assert n >= 20  # non-trivial
    assert g  # non-empty


def test_golden_answer_empty_source():
    assert golden_answer_from("Q?", "") == ""


def test_golden_answer_truncates_runaway_single_sentence():
    long = "word " * 200
    g = golden_answer_from("Q?", long)
    assert len(g.split()) <= GOLDEN_MAX_WORDS


def test_build_faq_templated_shape():
    faq = build_faq_templated(_KEYWORDS, ["We help with plumbing."])
    assert len(faq) == 5
    assert all(set(item) == {"q", "a"} for item in faq)
    assert all(item["q"].strip() and item["a"].strip() for item in faq)


def test_geo_enrich_on_llm_error_uses_template(monkeypatch):
    """With use_llm=True the free local LLM (LM Studio) fires unconditionally.
    When the call errors the FAQ still lands from the deterministic template
    (via `build_faq_via_llm`'s except-return-[] path) and used_llm=False."""
    import backend.common.llm_client as llm

    def _boom(**kwargs):
        raise RuntimeError("stub_lm_studio_down")

    # geo_format does `from backend.common.llm_client import anthropic_messages`
    # at call time -> patch the source module.
    monkeypatch.setattr(llm, "anthropic_messages", _boom, raising=False)
    geo = geo_enrich(_DRAFTS, _KEYWORDS, use_llm=True)
    assert geo.used_llm is False
    assert geo.golden_answer
    assert len(geo.faq) >= 5
    # answer-first markdown leads with the H1 then the bolded golden answer.
    assert geo.geo_markdown.startswith("# Emergency Plumbing You Can Trust")
    assert "**" in geo.geo_markdown
    assert "## Frequently Asked Questions" in geo.geo_markdown


def test_geo_enrich_use_llm_false_skips_llm():
    geo = geo_enrich(_DRAFTS, _KEYWORDS, use_llm=False)
    assert geo.used_llm is False
    assert len(geo.faq) == 5


def test_geo_enrich_empty_drafts_safe():
    geo = geo_enrich({}, [], use_llm=False)
    assert isinstance(geo.faq, list) and len(geo.faq) == 5
    assert isinstance(geo.geo_markdown, str)


def test_tolerant_json_array_variants():
    assert _tolerant_json_array('[{"q":"a","a":"b"}]') == [{"q": "a", "a": "b"}]
    assert _tolerant_json_array('```json\n[{"q":"a","a":"b"}]\n```') == [{"q": "a", "a": "b"}]
    assert _tolerant_json_array("prefix [1,2,3] suffix") == [1, 2, 3]
    assert _tolerant_json_array("not json") is None
    assert _tolerant_json_array('{"not":"array"}') is None
