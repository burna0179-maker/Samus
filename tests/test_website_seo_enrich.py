"""SEO + security enrichment: package builder, deterministic gate, live gate."""

from __future__ import annotations

from types import SimpleNamespace


from backend.website import seo_enrich
from backend.website import stages as stages_mod
from backend.website.models import WebsiteBrief, WebsiteOrder, WebsitePage
from backend.website.state import WebsiteBuildState


def _brief(**over):
    base = dict(
        business_name="Sample Cleaning",
        business_description="Family-owned professional house-cleaning service offering regular and one-time deep cleaning for commercial and residential spaces, including Airbnb turnovers.",
        industry="House Cleaning, Residential Cleaning, Airbnb Turnover",
        contact_email="hello@mighty.test",
        contact_phone="<phone>",
        address="<street>, <city>, <state> 97624",
        brand_colors=["#0E7C8B", "#5CB544", "#F2EC4F"],
        pages=[
            WebsitePage(
                slug="home", title="Home", content={"intro": "If you want a mighty clean, call us."}
            ),
            WebsitePage(
                slug="about", title="About", content={"body": "Family owned, 10 years experience."}
            ),
            WebsitePage(
                slug="services",
                title="Services",
                content={"list": "Deep Cleaning | Airbnb Turnovers"},
            ),
            WebsitePage(slug="contact", title="Contact", content={"body": "Call <phone>."}),
        ],
    )
    base.update(over)
    return WebsiteBrief(**base)


# ---------------------------------------------------------------------------
# build_seo_package
# ---------------------------------------------------------------------------


def test_package_has_localbusiness_and_organization_jsonld():
    pkg = seo_enrich.build_seo_package(_brief(), public_url="https://x.wixstudio.com/mysite")
    types = {j.get("@type") for j in pkg["jsonld"]}
    assert "LocalBusiness" in types and "Organization" in types
    lb = next(j for j in pkg["jsonld"] if j["@type"] == "LocalBusiness")
    assert lb["name"] == "Sample Cleaning"
    assert lb["telephone"] == "<phone>"
    assert lb["address"]["addressLocality"] == "<city>"
    assert lb["address"]["addressRegion"] == "OR"
    assert lb.get("areaServed", {}).get("name") == "<city>"
    assert lb.get("makesOffer")  # services mapped to offers
    assert "application/ld+json" in pkg["jsonld_script"]


def test_parse_address_ignores_non_zip_postal_token():
    # a parenthetical community note must not become a bogus postalCode
    a = seo_enrich._parse_address("29439 Easy Street, Klamath Falls, OR (Rocky Point)")
    assert a["city"] == "Klamath Falls"
    assert a["region"] == "OR"
    assert a["postal_code"] == ""
    # a real ZIP is still captured
    b = seo_enrich._parse_address("<street>, <city>, <state> 97624")
    assert b["postal_code"] == "97624"


def test_per_page_meta_well_formed():
    pkg = seo_enrich.build_seo_package(_brief(), public_url="https://x.wixstudio.com/mysite")
    assert set(pkg["page_meta"]) == {"home", "about", "services", "contact"}
    for slug, m in pkg["page_meta"].items():
        assert 0 < len(m["title"]) <= 60, f"{slug} title len {len(m['title'])}"
        assert 120 <= len(m["description"]) <= 160, f"{slug} desc len {len(m['description'])}"
        assert m["keywords"]


def test_package_passes_its_own_check():
    pkg = seo_enrich.build_seo_package(_brief(), public_url="https://x.wixstudio.com/mysite")
    assert seo_enrich.check_seo_package(pkg) == []


def test_check_flags_bad_package():
    bad = {
        "page_meta": {
            "home": {"title": "x" * 80, "description": "short", "keywords": "a"},
        },
        "jsonld": [],
    }
    problems = seo_enrich.check_seo_package(bad)
    assert any("title" in p for p in problems)
    assert any("meta description" in p for p in problems)
    assert any("LocalBusiness" in p for p in problems)


def test_no_em_dash_in_meta():
    b = _brief(
        business_description="Cleaning — fast — reliable service for homes and rentals nearby."
    )
    pkg = seo_enrich.build_seo_package(b, public_url="https://x.wixstudio.com/mysite")
    blob = " ".join(m["title"] + m["description"] for m in pkg["page_meta"].values())
    assert "—" not in blob and "–" not in blob


# ---------------------------------------------------------------------------
# live gate
# ---------------------------------------------------------------------------


def test_live_gate_passes_clean_site():
    live = {"ok": True, "seo_score": 88, "security_grade": "A", "critical_findings": []}
    problems = seo_enrich.live_gate_violations(
        live, seo_min_score=70, security_min_grade="B", seo_gate="enforce", security_gate="enforce"
    )
    assert problems == []


def test_live_gate_blocks_low_seo_and_bad_security():
    live = {
        "ok": True,
        "seo_score": 40,
        "security_grade": "D",
        "critical_findings": ["tls_certificate_expired"],
    }
    problems = seo_enrich.live_gate_violations(
        live, seo_min_score=70, security_min_grade="B", seo_gate="enforce", security_gate="enforce"
    )
    assert any("seo_score" in p for p in problems)
    assert any("security grade" in p for p in problems)
    assert any("critical" in p for p in problems)


def test_live_gate_unavailable_is_fail_closed():
    problems = seo_enrich.live_gate_violations(
        {"ok": False, "reason": "audit_failed:Timeout"},
        seo_min_score=70,
        security_min_grade="B",
        seo_gate="enforce",
        security_gate="enforce",
    )
    assert problems and "unavailable" in problems[0]


def test_evaluate_live_reads_audit(monkeypatch):
    fake = SimpleNamespace(
        seo_score=82,
        issues=[1, 2],
        findings={
            "security": {
                "grade": "B",
                "findings": [
                    {"id": "x", "severity": "medium"},
                    {"id": "y", "severity": "critical"},
                ],
                "infrastructure_health": {"score": 0.8},
            }
        },
    )
    monkeypatch.setattr("backend.seo.audit.audit_url", lambda *a, **k: fake)
    out = seo_enrich.evaluate_live("https://x.test", keywords=["cleaning"], industry="cleaning")
    assert out["ok"] and out["seo_score"] == 82 and out["security_grade"] == "B"
    assert out["critical_findings"] == ["y"]


def test_evaluate_live_failure_is_fail_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("backend.seo.audit.audit_url", boom)
    out = seo_enrich.evaluate_live("https://x.test")
    assert out["ok"] is False and "audit_failed" in out["reason"]


# ---------------------------------------------------------------------------
# stage wiring
# ---------------------------------------------------------------------------


def _settings(**over):
    base = dict(
        website_seo_enrich_enabled=True,
        website_seo_gate="enforce",
        website_security_gate="enforce",
        website_seo_min_score=70,
        website_security_min_grade="B",
        website_live_publish_enabled=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(brief, settings, **state_over):
    order = WebsiteOrder(customer_name="Harmony", brief=brief)
    st = WebsiteBuildState(order_id="wb-x", order=order, site_id="site-1", **state_over)
    return stages_mod.StageContext(
        state=st, order=order, settings=settings, live_publish_enabled=True
    ), st


def test_seo_stage_enforce_passes_and_stores_package():
    ctx, st = _ctx(_brief(public_url="https://x.wixstudio.com/mysite"), _settings())
    res = stages_mod._seo_stage(ctx)
    assert res.ok
    assert res.detail["seo_package"]["jsonld"]
    assert res.detail["seo_problems"] == []


def test_seo_stage_disabled_is_passthrough():
    ctx, _ = _ctx(_brief(), _settings(website_seo_enrich_enabled=False))
    res = stages_mod._seo_stage(ctx)
    assert res.ok and res.detail == {"seo_enrich": "disabled"}


def test_deliver_gate_blocks_when_public_url_missing():
    ctx, _ = _ctx(_brief(public_url=""), _settings())  # no public_url + enforce
    res = stages_mod._deliver_stage(ctx)
    assert res.parked and res.park_reason == "public_url_unknown"


def test_deliver_gate_blocks_failing_live_audit(monkeypatch):
    ctx, _ = _ctx(_brief(public_url="https://x.wixstudio.com/mysite"), _settings())
    monkeypatch.setattr(
        seo_enrich,
        "evaluate_live",
        lambda *a, **k: {
            "ok": True,
            "seo_score": 30,
            "security_grade": "F",
            "critical_findings": [],
        },
    )
    res = stages_mod._deliver_stage(ctx)
    assert res.parked and res.park_reason.startswith("quality_gate_failed:")


def test_deliver_gate_passes_clean_live_audit(monkeypatch):
    ctx, _ = _ctx(_brief(public_url="https://x.wixstudio.com/mysite"), _settings())
    monkeypatch.setattr(
        seo_enrich,
        "evaluate_live",
        lambda *a, **k: {
            "ok": True,
            "seo_score": 92,
            "security_grade": "A",
            "critical_findings": [],
        },
    )
    res = stages_mod._deliver_stage(ctx)
    assert res.ok and res.detail["delivered_url"]
