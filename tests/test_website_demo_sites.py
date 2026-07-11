"""Tests for backend.website.demo_sites — offline (deploy + LLM monkeypatched)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.prospecting.csv_export import write_call_list
from backend.prospecting.models import ProspectRecord
from backend.website import demo_sites
from backend.website.demo_sites import (
    build_demo_sites,
    demo_project_slug,
    enrich_brief,
    load_no_website_prospects,
)
from backend.website.deploy_cloudflare import DeployResult
from backend.website.no_website_classifier import classify

RUN_DATE = date(2026, 6, 11)


def _rec(**kwargs) -> ProspectRecord:
    defaults = dict(
        prospect_id="p1",
        company_name="Acme Plumbing",
        phone="555-1234",
        industry="plumbing",
        city="Yuba City",
        state="CA",
        lead_score=75,
        website_status="no_website",
        business_description="Family owned plumbing for homes and businesses",
        review_rating="4.8",
        review_count="120",
    )
    defaults.update(kwargs)
    return ProspectRecord(**defaults)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        website_demo_sites_enabled=True,
        anthropic_api_key="",
        cloudflare_api_token="",
        cloudflare_account_id="",
        demo_sites_ceiling_usd=5.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def artifact_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Belt-and-braces: no test may reach Cloudflare even if creds leak in."""
    monkeypatch.setattr(
        "backend.website.deploy_cloudflare.create_project",
        lambda **kw: {"status": 409},
    )
    monkeypatch.setattr(
        "backend.website.deploy_cloudflare.deploy_via_wrangler",
        lambda *a, **kw: DeployResult(False, error="network disabled in tests"),
    )


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """LM Studio is local and reachable in dev, but these tests assert
    deterministic $0 behaviour. Disable the LLM client unless a test
    opts in by replacing generate_site_content directly (see
    _fake_content_gen)."""
    from backend.common.llm_client import LlmCallError

    def _unavailable(*a, **kw):
        raise LlmCallError("llm disabled in test")

    monkeypatch.setattr(
        "backend.website.content_gen.anthropic_messages", _unavailable
    )


# ---------------------------------------------------------------------------
# CSV loading / filtering
# ---------------------------------------------------------------------------

def test_load_filters_no_website_only(artifact_root):
    records = [
        _rec(prospect_id="a", company_name="No Site Co"),
        _rec(prospect_id="b", company_name="Live Co", website_status="live",
             website_url="https://live.example"),
        _rec(prospect_id="c", company_name="Parked Co", website_status="parked"),
    ]
    write_call_list(records, run_date=RUN_DATE)
    loaded = load_no_website_prospects(run_date=RUN_DATE)
    assert [r.prospect_id for r in loaded] == ["a"]
    assert loaded[0].business_description == records[0].business_description
    assert loaded[0].review_rating == "4.8"


def test_load_deneutralizes_formula_prefixed_cells(artifact_root):
    # csv_export prefixes "'" on formula-trigger cells (CWE-1236); reading
    # back must restore the original value.
    rec = _rec(company_name="=Evil Formula Co")
    write_call_list([rec], run_date=RUN_DATE)
    loaded = load_no_website_prospects(run_date=RUN_DATE)
    assert loaded[0].company_name == "=Evil Formula Co"


def test_load_tolerates_missing_and_extra_columns(artifact_root, tmp_path):
    path = tmp_path / "partial.csv"
    path.write_text(
        "company_name,phone,website_status,lead_score,mystery_column\n"
        "Tiny Co,555-9999,no_website,80,whatever\n",
        encoding="utf-8",
    )
    loaded = load_no_website_prospects(csv_path=path)
    assert len(loaded) == 1
    assert loaded[0].company_name == "Tiny Co"
    assert loaded[0].lead_score == 80
    assert loaded[0].city == ""  # missing column -> model default


def test_load_missing_file_raises(artifact_root):
    with pytest.raises(FileNotFoundError):
        load_no_website_prospects(run_date=date(1999, 1, 1))


# ---------------------------------------------------------------------------
# brief enrichment
# ---------------------------------------------------------------------------

def test_enrich_brief_carries_reviews_description_and_contact():
    rec = _rec()
    brief = enrich_brief(classify(rec).brief_draft, rec)
    by_slug = {p.slug: p.content for p in brief.pages}
    assert "Rated 4.8★ by 120 customers" == by_slug["home"]["subheadline"]
    assert "Family owned plumbing" in by_slug["about"]["body"]
    assert "Yuba City, CA" in by_slug["about"]["body"]
    assert "555-1234" in by_slug["contact"]["body"]
    assert "Plumbing" in by_slug["home"]["headline"]


def test_enrich_brief_never_fabricates_trust_line():
    rec = _rec(review_rating="", review_count="")
    brief = enrich_brief(classify(rec).brief_draft, rec)
    by_slug = {p.slug: p.content for p in brief.pages}
    assert "subheadline" not in by_slug["home"]


# ---------------------------------------------------------------------------
# slug sanitization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Acme Plumbing", "demo-acme-plumbing"),
    ("  Bob's HVAC & Sons!  ", "demo-bob-s-hvac-sons"),
    ("ÜBER--Clean__LLC", "demo-ber-clean-llc"),
    ("", "demo-site"),
    ("!!!", "demo-site"),
])
def test_demo_project_slug(name, expected):
    assert demo_project_slug(name) == expected


def test_demo_project_slug_length_and_determinism():
    long = demo_project_slug("x" * 200)
    assert len(long) <= 58
    assert long == demo_project_slug("x" * 200)
    assert not long.endswith("-")


# ---------------------------------------------------------------------------
# flag gate
# ---------------------------------------------------------------------------

def test_flag_off_refuses(artifact_root):
    with pytest.raises(RuntimeError, match="website_demo_sites_enabled"):
        build_demo_sites([_rec()], settings=_settings(website_demo_sites_enabled=False))


# ---------------------------------------------------------------------------
# build run — deterministic ($0), deploy skipped without creds
# ---------------------------------------------------------------------------

def test_build_no_creds_files_only(artifact_root):
    run = build_demo_sites([_rec()], settings=_settings(), run_date=RUN_DATE)
    assert run.built == 1 and run.failed == 0 and run.deployed == 0
    assert run.cumulative_cost_usd == 0.0
    r = run.results[0]
    assert r.deployed is False
    assert r.deploy_skipped_reason == "missing credentials"
    assert r.project == "demo-acme-plumbing"
    assert (Path(r.output_dir) / "index.html").exists()
    assert r.pitch_hook  # classifier hook carried for the call sheet


def test_build_deploy_disabled(artifact_root):
    run = build_demo_sites([_rec()], settings=_settings(), deploy=False, run_date=RUN_DATE)
    assert run.results[0].deploy_skipped_reason == "disabled"
    assert run.deployed == 0


def test_build_deploy_success_per_project(artifact_root, monkeypatch):
    seen: list[str] = []

    def fake_deploy(build_dir, *, project, api_token, account_id, **kw):
        seen.append(project)
        return DeployResult(True, url=f"https://{project}.pages.dev", project=project)

    monkeypatch.setattr("backend.website.deploy_cloudflare.deploy_via_wrangler", fake_deploy)
    settings = _settings(cloudflare_api_token="tok", cloudflare_account_id="acct")
    run = build_demo_sites(
        [_rec(prospect_id="a"), _rec(prospect_id="b", company_name="Beta Roofing")],
        settings=settings, run_date=RUN_DATE,
    )
    assert run.deployed == 2
    assert seen == ["demo-acme-plumbing", "demo-beta-roofing"]
    assert run.results[1].url == "https://demo-beta-roofing.pages.dev"


# ---------------------------------------------------------------------------
# cost ceiling — HARD, checked before each LLM call
# ---------------------------------------------------------------------------

def _fake_content_gen(monkeypatch, calls: list[str]):
    """generate_site_content stand-in: 'uses' the LLM iff an api_key arrives."""
    def fake(brief, *, settings, api_key=None):
        used = bool(api_key)
        if used:
            calls.append(brief.business_name)
        return brief, {"llm_used": used, "sanitized": False}

    monkeypatch.setattr("backend.website.content_gen.generate_site_content", fake)


def test_ceiling_stops_llm_but_run_continues(artifact_root, monkeypatch):
    calls: list[str] = []
    _fake_content_gen(monkeypatch, calls)
    prospects = [_rec(prospect_id=str(i), company_name=f"Co {i}") for i in range(4)]
    # estimate is $0.05/call -> ceiling $0.12 allows exactly 2 calls
    run = build_demo_sites(
        prospects, ceiling_usd=0.12,
        settings=_settings(anthropic_api_key="k"), run_date=RUN_DATE,
    )
    assert run.llm_calls == 2
    assert len(calls) == 2
    assert run.llm_stopped_at_ceiling is True
    assert run.cumulative_cost_usd <= 0.12  # NEVER exceeds the ceiling
    assert run.built == 4                   # deterministic path finished the rest
    assert [r.llm_used for r in run.results] == [True, True, False, False]


def test_no_llm_flag_means_zero_cost(artifact_root, monkeypatch):
    calls: list[str] = []
    _fake_content_gen(monkeypatch, calls)
    run = build_demo_sites(
        [_rec()], use_llm_copy=False,
        settings=_settings(anthropic_api_key="k"), run_date=RUN_DATE,
    )
    assert calls == []
    assert run.cumulative_cost_usd == 0.0


# Note: an older `test_no_api_key_means_no_llm` was removed when the
# LLM backend swapped from Anthropic (api-key-gated) to LM Studio /
# OpenAI (the api-key gate no longer applies; use_llm_copy=False is
# the equivalent operator switch — see test_no_llm_flag_means_zero_cost).


# ---------------------------------------------------------------------------
# per-prospect fault isolation
# ---------------------------------------------------------------------------

def test_one_prospect_failure_is_isolated(artifact_root, monkeypatch):
    real = demo_sites.classify

    def exploding_classify(record):
        if record.company_name == "Boom Co":
            raise ValueError("synthetic classifier failure")
        return real(record)

    monkeypatch.setattr(demo_sites, "classify", exploding_classify)
    run = build_demo_sites(
        [_rec(prospect_id="a"), _rec(prospect_id="b", company_name="Boom Co"),
         _rec(prospect_id="c", company_name="Gamma Dental")],
        settings=_settings(), run_date=RUN_DATE,
    )
    assert run.built == 2 and run.failed == 1
    assert "synthetic classifier failure" in run.results[1].error
    assert run.results[2].error == ""  # later prospects unaffected


def test_non_gap_prospect_recorded_not_crashed(artifact_root):
    # verify_presence=False isolates the classify() non-gap path (the presence
    # gate — which skips a live-site prospect first — is tested separately in
    # tests/test_presence_check.py and below).
    run = build_demo_sites(
        [_rec(website_status="live", website_url="https://x.example")],
        settings=_settings(), run_date=RUN_DATE, verify_presence=False,
    )
    assert run.failed == 1
    assert "not a website-gap prospect" in run.results[0].error


def test_presence_gate_skips_business_with_a_site(artifact_root):
    # The insurance gate skips (never builds) a prospect that already has a
    # website, without reaching classify/build.
    run = build_demo_sites(
        [_rec(website_status="live", website_url="https://x.example")],
        settings=_settings(), run_date=RUN_DATE, verify_presence=True,
    )
    assert run.built == 0
    assert run.skipped_has_site == 1
    assert "already carries a website" in run.results[0].skipped_reason


# ---------------------------------------------------------------------------
# run summaries
# ---------------------------------------------------------------------------

def test_summary_files_written(artifact_root):
    run = build_demo_sites([_rec()], settings=_settings(), run_date=RUN_DATE)
    jp, tp = Path(run.json_path), Path(run.txt_path)
    assert jp.exists() and tp.exists()
    assert jp.name == "demo_sites_2026-06-11.json"
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["ceiling_usd"] == 5.0
    assert data["results"][0]["company_name"] == "Acme Plumbing"
    sheet = tp.read_text(encoding="utf-8")
    assert "Acme Plumbing" in sheet
    assert "555-1234" in sheet
    assert "hook" in sheet
    assert "$0.00" in sheet

# ---------------------------------------------------------------------------
# industry service catalog (operator HVAC spec)
# ---------------------------------------------------------------------------

def test_industry_services_per_industry_differ():
    from backend.website.demo_sites import industry_services
    hvac, hvac_offer = industry_services("hvac contractor")
    acct, _ = industry_services("accounting firm")
    assert "AC Repair" in hvac and "Furnace Installation" in hvac
    assert "Tax Preparation" in acct
    assert set(hvac) != set(acct)
    assert "estimate" in hvac_offer.lower()
    assert 4 <= len(hvac) <= 6 and 4 <= len(acct) <= 6


@pytest.mark.parametrize("industry,expect", [
    ("HVAC Contractor", "AC Repair"),
    ("Plumber", "Drain Cleaning"),
    ("roofing contractor", "Roof Repair"),
    ("Electrician", "Panel Upgrades"),
    ("Real Estate Agency", "Home Buying"),
    ("car dealership", "Trade-In Appraisals"),
    ("Dentist", "Teeth Whitening"),
])
def test_industry_services_keyword_resolution(industry, expect):
    from backend.website.demo_sites import industry_services
    services, _ = industry_services(industry)
    assert expect in services


def test_industry_services_generic_fallback():
    from backend.website.demo_sites import industry_services
    services, offer = industry_services("interpretive dance studio")
    assert services and offer
    assert "AC Repair" not in services  # never the wrong trade's catalog


def test_enrich_brief_seeds_service_catalog_and_promo():
    rec = _rec(industry="hvac contractor")
    brief = enrich_brief(classify(rec).brief_draft, rec)
    by_slug = {p.slug: p.content for p in brief.pages}
    assert "AC Repair" in by_slug["services"]["list"]
    assert "Furnace Installation" in by_slug["services"]["list"]
    assert "555-1234" in by_slug["home"]["promo"]  # call-today band uses real phone
    assert "estimate" in by_slug["home"]["promo"].lower()


def test_enrich_brief_trust_line_and_placeholders_honest():
    rec = _rec()
    brief = enrich_brief(classify(rec).brief_draft, rec)
    home = {p.slug: p.content for p in brief.pages}["home"]
    # real Google review data -> trust line, attributed
    assert home["trust_line"] == "Rated 4.8★ by 120 customers on Google"
    # owner-supplied material renders as labeled placeholders, never invented
    assert "reviews" in home["testimonials_placeholder"].lower()
    assert "license" in home["credentials_placeholder"].lower()
    assert "projects" in home["portfolio_placeholder"].lower()


def test_enrich_brief_no_reviews_means_no_trust_line():
    rec = _rec(review_rating="", review_count="")
    brief = enrich_brief(classify(rec).brief_draft, rec)
    home = {p.slug: p.content for p in brief.pages}["home"]
    assert "trust_line" not in home  # section degrades, nothing fabricated


# ---------------------------------------------------------------------------
# rendered demo page — operator HVAC spec end-to-end (deterministic $0 path)
# ---------------------------------------------------------------------------

def _built_html(artifact_root, **rec_over) -> str:
    run = build_demo_sites([_rec(**rec_over)], settings=_settings(),
                           deploy=False, run_date=RUN_DATE)
    assert run.built == 1 and run.cumulative_cost_usd == 0.0
    return (Path(run.results[0].output_dir) / "index.html").read_text(encoding="utf-8")


def test_demo_page_click_to_call_in_header_and_hero(artifact_root):
    html = _built_html(artifact_root, industry="hvac contractor")
    # header nav + hero CTA + promo band + contact all click-to-call
    assert html.count('href="tel:5551234"') >= 3
    assert "Call 555-1234" in html


def test_demo_page_industry_services_promo_and_placeholders(artifact_root):
    html = _built_html(artifact_root, industry="hvac contractor")
    assert "AC Repair" in html and "Duct Cleaning" in html
    assert 'id="promo"' in html and "call 555-1234 today" in html
    assert html.count("Demo preview") >= 3      # reviews + credentials + portfolio
    assert "Your customer reviews featured here" in html
    assert "license &amp; certifications" in html.lower()
    assert 'id="portfolio"' in html and 'id="reviews"' in html


def test_demo_page_real_trust_line_only(artifact_root):
    html = _built_html(artifact_root, industry="hvac contractor")
    assert "Rated 4.8★ by 120 customers on Google" in html


def test_demo_page_no_reviews_degrades_without_fabrication(artifact_root):
    html = _built_html(artifact_root, industry="hvac contractor",
                       review_rating="", review_count="", business_description="")
    assert "Rated" not in html  # no invented social proof anywhere
    # the placeholder cards still demo the sections
    assert "Your customer reviews featured here" in html


def test_demo_page_two_click_nav(artifact_root):
    html = _built_html(artifact_root, industry="hvac contractor")
    header = html.split("</header>")[0]
    for label in ("Services", "About", "Contact"):
        assert label in header, label


def test_demo_page_differs_by_industry(artifact_root):
    hvac = _built_html(artifact_root, industry="hvac contractor")
    acct = _built_html(artifact_root, prospect_id="p2", company_name="Ledger Co",
                       industry="accounting firm")
    assert "AC Repair" in hvac and "AC Repair" not in acct
    assert "Tax Preparation" in acct and "Tax Preparation" not in hvac
