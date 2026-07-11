"""Taste-governed website content generation (the codegen) + the generate stage.

Covers the deterministic ($0-budget) path, operator-content preservation,
default-page seeding, the LLM path with fail-closed taste sanitation, and the
generate stage wired into the walk. No network: the Anthropic path is
monkeypatched; the deterministic path needs no key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.website import content_gen
from backend.website.models import WebsiteBrief, WebsiteOrder, WebsitePage
from backend.website.stages import StageContext, _generate_stage
from backend.website.state import WebsiteBuildState


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


def _settings(**over):
    base = dict(
        website_content_generation_enabled=True,
        website_content_model="claude-haiku-4-5-20251001",
        website_content_max_tokens=1200,
        anthropic_api_key="",  # legacy field; the LM Studio backend ignores it
    )
    base.update(over)
    return SimpleNamespace(**base)


def _brief(pages=None, **over):
    kwargs = dict(
        business_name="Mackabee Catering",
        business_description="Home cooked catering for local events",
        industry="catering",
    )
    kwargs.update(over)
    if pages is not None:
        kwargs["pages"] = pages
    return WebsiteBrief(**kwargs)


_DASHES = ("—", "–")


def _no_dashes(brief: WebsiteBrief) -> bool:
    blob = " ".join(v for p in brief.pages for v in p.content.values())
    return not any(d in blob for d in _DASHES)


# ---------------------------------------------------------------------------
# deterministic ($0 budget) path
# ---------------------------------------------------------------------------


def test_deterministic_fills_all_fields_on_llm_error(monkeypatch):
    """When the LLM call errors (transport / budget / parse), the deterministic
    templated path fills every STANDARD_FIELD with taste-audited copy. The LLM
    backend is local LM Studio (free, unconditionally invoked); this exercises
    the fallback that fires when LM Studio itself is unavailable."""

    def _boom(**kwargs):
        raise content_gen.LlmCallError("stub_lm_studio_down")

    monkeypatch.setattr(content_gen, "anthropic_messages", _boom)

    enriched, report = content_gen.generate_site_content(
        _brief(pages=[WebsitePage(slug="home", title="Home")]),
        settings=_settings(),
    )
    assert report["llm_used"] is False
    assert report["taste_audit"]["passed"] is True
    home = enriched.pages[0].content
    for field in content_gen.STANDARD_FIELDS:
        assert home.get(field), f"missing {field}"
    assert _no_dashes(enriched)


def test_default_page_set_when_brief_has_no_pages():
    enriched, report = content_gen.generate_site_content(_brief(pages=[]), settings=_settings())
    slugs = [p.slug for p in enriched.pages]
    assert slugs == ["home", "about", "services", "contact"]
    assert all(p.content.get("headline") for p in enriched.pages)


def test_operator_supplied_content_is_preserved():
    page = WebsitePage(slug="home", title="Home", content={"headline": "My Exact Headline"})
    enriched, _ = content_gen.generate_site_content(_brief(pages=[page]), settings=_settings())
    home = enriched.pages[0].content
    assert home["headline"] == "My Exact Headline"  # operator wins
    assert home.get("body")  # the rest still filled


def test_single_site_cta_avoids_duplicate_intent():
    enriched, report = content_gen.generate_site_content(_brief(pages=[]), settings=_settings())
    ctas = {p.content["cta"] for p in enriched.pages}
    assert ctas == {"Get started"}
    assert report["taste_audit"]["passed"] is True


# ---------------------------------------------------------------------------
# LLM path (monkeypatched) + fail-closed taste sanitation
# ---------------------------------------------------------------------------


def test_llm_path_used_when_key_present(monkeypatch):
    def fake_llm(**kwargs):
        return (
            '{"pages": {"home": {"headline": "Catering done right", '
            '"subheadline": "Fresh local food", "body": "We cater events.", '
            '"cta": "Get started", "meta_description": "Local catering."}}}',
            {"input_tokens": 10, "output_tokens": 20},
        )

    monkeypatch.setattr(content_gen, "anthropic_messages", fake_llm)
    enriched, report = content_gen.generate_site_content(
        _brief(pages=[WebsitePage(slug="home", title="Home")]),
        settings=_settings(anthropic_api_key="sk-test"),
    )
    assert report["llm_used"] is True
    assert enriched.pages[0].content["headline"] == "Catering done right"


def test_llm_em_dash_is_sanitized_fail_closed(monkeypatch):
    def fake_llm(**kwargs):
        return (
            '{"pages": {"home": {"headline": "Best in town — truly", '
            '"subheadline": "Fresh – local", "body": "We cater.", '
            '"cta": "Get started", "meta_description": "Local catering."}}}',
            {"input_tokens": 1, "output_tokens": 1},
        )

    monkeypatch.setattr(content_gen, "anthropic_messages", fake_llm)
    enriched, report = content_gen.generate_site_content(
        _brief(pages=[WebsitePage(slug="home", title="Home")]),
        settings=_settings(anthropic_api_key="sk-test"),
    )
    # The em-dash never reaches the customer's site.
    assert _no_dashes(enriched)
    assert report["sanitized"] is True
    assert report["taste_audit"]["passed"] is True


def test_llm_unparseable_falls_back_to_deterministic(monkeypatch):
    monkeypatch.setattr(content_gen, "anthropic_messages", lambda **k: ("not json at all", {}))
    monkeypatch.setattr(content_gen, "record_outcome", lambda *a, **k: None)
    enriched, report = content_gen.generate_site_content(
        _brief(pages=[WebsitePage(slug="home", title="Home")]),
        settings=_settings(anthropic_api_key="sk-test"),
    )
    assert report["llm_used"] is False  # parse failed -> fallback
    assert report["taste_audit"]["passed"] is True
    assert enriched.pages[0].content.get("headline")


def test_llm_call_error_falls_back(monkeypatch):
    def boom(**kwargs):
        raise content_gen.LlmCallError("transport down")

    monkeypatch.setattr(content_gen, "anthropic_messages", boom)
    enriched, report = content_gen.generate_site_content(
        _brief(pages=[WebsitePage(slug="home", title="Home")]),
        settings=_settings(anthropic_api_key="sk-test"),
    )
    assert report["llm_used"] is False
    assert report["taste_audit"]["passed"] is True


# ---------------------------------------------------------------------------
# the generate stage
# ---------------------------------------------------------------------------


def _ctx(brief, settings):
    order = WebsiteOrder(customer_name="Sample Customer", brief=brief)
    state = WebsiteBuildState(order_id="wb-1", order=order)
    return StageContext(state=state, order=order, settings=settings), order


def test_generate_stage_fills_brief_and_reports(monkeypatch):
    """The generate stage enriches the brief and records a truthful report even
    when the LLM is unavailable -- LM Studio down here is stubbed so the whole
    stage traverses the deterministic fallback path."""

    def _boom(**kwargs):
        raise content_gen.LlmCallError("stub_lm_studio_down")

    monkeypatch.setattr(content_gen, "anthropic_messages", _boom)

    ctx, order = _ctx(_brief(pages=[WebsitePage(slug="home", title="Home")]), _settings())
    res = _generate_stage(ctx)
    assert res.ok
    assert res.detail["taste_passed"] is True
    assert res.detail["llm_used"] is False
    # the order's brief is enriched in place for the downstream content stage
    assert order.brief.pages[0].content.get("body")
    assert "generation_report" in res.detail


def test_generate_stage_disabled_is_passthrough():
    brief = _brief(pages=[WebsitePage(slug="home", title="Home", content={})])
    ctx, order = _ctx(brief, _settings(website_content_generation_enabled=False))
    res = _generate_stage(ctx)
    assert res.ok
    assert res.detail == {"content_generation": "disabled"}
    # brief copy untouched
    assert order.brief.pages[0].content == {}


def test_generate_stage_seeds_taste_profile_in_report():
    ctx, _ = _ctx(_brief(pages=[]), _settings())
    res = _generate_stage(ctx)
    report = res.detail["generation_report"]
    assert "taste_profile" in report
    assert "dials" in report["taste_profile"]
    assert report["pages_generated"] == ["home", "about", "services", "contact"]
