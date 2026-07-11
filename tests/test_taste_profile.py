"""Tests for backend.taste.profile (brief inference + dial resolution)."""
from __future__ import annotations

from backend.taste.models import TasteDials, TasteProfile
from backend.taste.profile import (
    build_profile,
    infer_design_read,
    resolve_dials,
    select_design_system,
)


def test_baseline_dials_when_no_signal():
    read = infer_design_read("a website")
    dials = resolve_dials(read)
    assert (dials.design_variance, dials.motion_intensity, dials.visual_density) == (8, 6, 4)


def test_trust_first_lowers_variance_and_motion():
    read = infer_design_read("a public-sector government services portal")
    dials = resolve_dials(read)
    assert dials.design_variance <= 4
    assert dials.motion_intensity <= 2
    assert "trust_first_regulated" in read.signals


def test_playful_agency_maxes_variance_and_motion():
    read = infer_design_read("a bold playful awwwards-style creative agency site")
    dials = resolve_dials(read)
    assert dials.design_variance == 10
    assert dials.motion_intensity == 9


def test_minimalist_signal_beats_premium_when_first():
    # signals are ordered most-specific first: minimalist_editorial before premium
    read = infer_design_read("a minimalist premium editorial brand")
    dials = resolve_dials(read)
    assert dials.visual_density == 3  # minimalist_editorial wins


def test_design_system_selection_official_package():
    ds = select_design_system("an enterprise Microsoft Fluent SaaS dashboard")
    assert ds["package"] == "@fluentui/react-components"
    assert "fluentui" in ds["install"]


def test_design_system_defaults_to_tailwind():
    ds = select_design_system("an indie AI startup landing page")
    assert ds["package"] == "tailwind-v4-native"


def test_dial_overrides_take_precedence():
    profile = build_profile("a playful agency site", dial_overrides={"motion_intensity": 2})
    assert profile.dials.motion_intensity == 2
    # the other dials still resolve from the brief
    assert profile.dials.design_variance == 10


def test_build_profile_carries_core_constraints():
    profile = build_profile("a premium cookware brand store")
    assert isinstance(profile, TasteProfile)
    assert any("em-dash" in c.lower() for c in profile.constraints)
    # premium-consumer palette guidance must warn about the banned beige family
    assert any("banned" in g.lower() for g in profile.palette_guidance)


def test_ambiguous_brief_flags_clarification():
    read = infer_design_read("site")
    assert read.needs_clarification is True


def test_dials_clamp_out_of_range_overrides():
    dials = TasteDials(design_variance=99, motion_intensity=-3, visual_density=4)
    assert dials.design_variance == 10
    assert dials.motion_intensity == 1


def test_design_read_one_liner_format():
    read = infer_design_read("a premium consumer wellness landing page")
    assert read.one_liner.startswith("Reading this as:")
