"""Tests for backend.taste.audit (deterministic Pre-Flight gate)."""

from __future__ import annotations

from backend.taste.audit import audit_deliverable, audit_text
from backend.taste.models import TasteAuditResult


def test_clean_markup_passes_with_high_score():
    html = (
        '<section><h1 class="text-6xl">Build faster operations</h1>'
        "<p>Automation-first orchestration with governed execution.</p>"
        '<a class="btn">Get started</a></section>'
    )
    r = audit_text(html, section_count=1)
    assert isinstance(r, TasteAuditResult)
    assert r.passed is True
    assert r.score >= 0.8
    assert r.grade == "A"
    assert r.fail_count == 0


def test_em_dash_is_hard_fail():
    r = audit_text("Our process — refined over years — ships fast.")
    assert r.passed is False
    assert r.fail_count >= 1
    assert any(v.check_id == "em_dash_ban" for v in r.violations)


def test_en_dash_separator_also_banned():
    r = audit_text("Selected work – 2018–2026")
    assert any(v.check_id == "em_dash_ban" for v in r.violations)
    assert r.passed is False


def test_banned_premium_palette_fails():
    css = "body { background:#f5f1ea; color:#1a1714; } .cta { background:#b08947; }"
    r = audit_text(css)
    assert any(v.check_id == "banned_palette" for v in r.violations)
    assert r.passed is False


def test_banned_palette_via_colors_hint():
    r = audit_text("<div>clean</div>", colors=["#B08947"])
    assert any(v.check_id == "banned_palette" for v in r.violations)


def test_banned_font_fails_inter_only_warns():
    r_serif = audit_text("font-family: Fraunces, serif;")
    assert any(v.check_id == "font_discipline" for v in r_serif.violations)
    assert r_serif.passed is False

    r_inter = audit_text("font-family: Inter, sans-serif;")
    assert any(v.check_id == "font_default_discouraged" for v in r_inter.violations)
    # Inter is discouraged, not banned -> still passes (no hard fail)
    assert r_inter.passed is True


def test_eyebrow_restraint_over_ratio_warns():
    # 6 eyebrows across 3 sections -> allowed ceil(3/3)=1 -> warn
    blocks = ""
    for i in range(3):
        blocks += (
            f'<section><span class="uppercase tracking-wide">Label {i}</span>'
            f'<span class="uppercase tracking-widest">Sub {i}</span><h2>Head {i}</h2></section>'
        )
    r = audit_text(blocks)
    assert any(v.check_id == "eyebrow_restraint" for v in r.violations)


def test_scroll_listener_is_hard_fail():
    code = 'window.addEventListener("scroll", () => setY(window.scrollY));'
    r = audit_text(code)
    assert any(v.check_id == "scroll_listener_ban" for v in r.violations)
    assert r.passed is False


def test_h_screen_warns():
    r = audit_text('<div class="h-screen flex">hero</div>')
    assert any(v.check_id == "viewport_stability" for v in r.violations)
    assert r.passed is True  # warn only


def test_duplicate_cta_intent_warns():
    r = audit_text("<a>Get in touch</a> ... <a>Contact us</a>")
    assert any(v.check_id == "duplicate_cta_intent" for v in r.violations)


def test_ai_tell_quietly_trusted_warns():
    r = audit_text("<p>Quietly trusted by leading teams</p>")
    assert any(v.check_id == "tell_quietly_trusted" for v in r.violations)


def test_audit_deliverable_pulls_document_field():
    r = audit_deliverable({"document": "Clean proposal copy with no tells.", "kind": "proposal"})
    assert r.passed is True


def test_audit_deliverable_empty_text_is_clean_skip():
    r = audit_deliverable({"document": ""})
    assert r.passed is True
    assert r.checks_run == []
    assert any("skipped" in line.lower() for line in r.rationale)


def test_score_monotonic_more_violations_lower_score():
    clean = audit_text("Clean copy.")
    dirty = audit_text("Clean copy with an em-dash — and #f5f1ea background.")
    assert dirty.score < clean.score
