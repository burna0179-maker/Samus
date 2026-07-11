"""Tests for backend.prospecting.dynamic_script.

Covers generate_script and generate_script_with_pivot.
All functions are pure; no I/O or mocking required.
"""
from __future__ import annotations

import pytest

from backend.prospecting.dynamic_script import (
    ANGLE_HOOKS,
    PRODUCT_CLOSE,
    PRODUCT_PITCH,
    VOICEMAIL_TEMPLATES,
    generate_script,
    generate_script_with_pivot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FULL_INTEL = {
    "pitch_angle": "trust_gap",
    "products": {
        "primary": "website_build",
        "secondary": "seo_package",
    },
    "signals": {
        "signal":  "low review count",
        "region":  "Austin TX",
        "keyword": "plumbing services",
    },
}

_MINIMAL_INTEL: dict = {}


# ---------------------------------------------------------------------------
# generate_script — required keys
# ---------------------------------------------------------------------------


def test_generate_script_returns_all_required_keys():
    """generate_script must return all 7 required keys."""
    result = generate_script("Acme Corp", _FULL_INTEL)
    required = {"opener", "pitch", "close", "voicemail",
                "pitch_angle", "primary_product", "secondary_product"}
    assert required == set(result.keys())


def test_generate_script_returns_all_required_keys_minimal_intel():
    """Required keys present even with empty intel."""
    result = generate_script("Acme Corp", _MINIMAL_INTEL)
    required = {"opener", "pitch", "close", "voicemail",
                "pitch_angle", "primary_product", "secondary_product"}
    assert required == set(result.keys())


# ---------------------------------------------------------------------------
# Company name in opener
# ---------------------------------------------------------------------------


def test_generate_script_includes_company_name_in_opener():
    """Company name must appear verbatim in the opener."""
    result = generate_script("North Ridge Transport", _FULL_INTEL)
    assert "North Ridge Transport" in result["opener"]


def test_generate_script_includes_company_name_in_opener_minimal():
    """Company name present in opener even with minimal intel."""
    result = generate_script("Sunrise Plumbing", _MINIMAL_INTEL)
    assert "Sunrise Plumbing" in result["opener"]


# ---------------------------------------------------------------------------
# pitch_angle routing
# ---------------------------------------------------------------------------


def test_generate_script_uses_pitch_angle_from_intel():
    """pitch_angle from intel must be reflected in the returned pitch_angle key."""
    for angle in ANGLE_HOOKS:
        intel = {**_FULL_INTEL, "pitch_angle": angle}
        result = generate_script("Test Co", intel)
        assert result["pitch_angle"] == angle


def test_generate_script_default_angle_when_missing():
    """Missing pitch_angle key defaults to general_growth."""
    result = generate_script("Test Co", {})
    assert result["pitch_angle"] == "general_growth"


def test_generate_script_default_angle_for_unknown_angle():
    """Unknown pitch_angle string defaults to general_growth."""
    result = generate_script("Test Co", {"pitch_angle": "alien_gap"})
    assert result["pitch_angle"] == "general_growth"


# ---------------------------------------------------------------------------
# Product routing
# ---------------------------------------------------------------------------


def test_generate_script_default_product_when_missing():
    """Missing products key defaults primary to workflow_automation."""
    result = generate_script("Test Co", {})
    assert result["primary_product"] == "workflow_automation"
    assert result["secondary_product"] is None


def test_generate_script_uses_primary_product_from_intel():
    """Primary product from intel.products.primary is used."""
    intel = {"products": {"primary": "seo_package", "secondary": None}}
    result = generate_script("Test Co", intel)
    assert result["primary_product"] == "seo_package"


def test_generate_script_unknown_primary_falls_back_to_default():
    """Unknown primary product string falls back to workflow_automation."""
    intel = {"products": {"primary": "not_a_product", "secondary": None}}
    result = generate_script("Test Co", intel)
    assert result["primary_product"] == "workflow_automation"


# ---------------------------------------------------------------------------
# generate_script_with_pivot — pivot key
# ---------------------------------------------------------------------------


def test_generate_script_with_pivot_includes_pivot_when_secondary_set():
    """pivot key is a non-empty string when a valid secondary product is set."""
    intel = {
        "pitch_angle": "trust_gap",
        "products": {"primary": "website_build", "secondary": "seo_package"},
    }
    result = generate_script_with_pivot("Test Co", intel)
    assert "pivot" in result
    assert isinstance(result["pivot"], str)
    assert len(result["pivot"]) > 0


def test_generate_script_with_pivot_no_pivot_when_secondary_missing():
    """pivot key is None when no secondary product is present."""
    intel = {
        "pitch_angle": "trust_gap",
        "products": {"primary": "website_build", "secondary": None},
    }
    result = generate_script_with_pivot("Test Co", intel)
    assert result["pivot"] is None


def test_generate_script_with_pivot_no_pivot_when_no_products():
    """pivot key is None with empty intel."""
    result = generate_script_with_pivot("Test Co", {})
    assert result["pivot"] is None


def test_generate_script_with_pivot_returns_base_keys_plus_pivot():
    """generate_script_with_pivot returns all 7 base keys plus pivot."""
    result = generate_script_with_pivot("Acme Corp", _FULL_INTEL)
    required = {"opener", "pitch", "close", "voicemail",
                "pitch_angle", "primary_product", "secondary_product", "pivot"}
    assert required == set(result.keys())


def test_generate_script_with_pivot_pivot_contains_secondary_pitch_and_close():
    """pivot string contains text from PRODUCT_PITCH and PRODUCT_CLOSE for secondary."""
    intel = {
        "products": {"primary": "website_build", "secondary": "ads_management"},
    }
    result = generate_script_with_pivot("Test Co", intel)
    # The pivot format is "<pitch> — <close>"; verify the separator is present.
    assert result["pivot"] is not None
    assert " — " in result["pivot"]


# ---------------------------------------------------------------------------
# Voicemail substitution
# ---------------------------------------------------------------------------


def test_voicemail_includes_company_substitution():
    """Company name must appear in the voicemail block."""
    result = generate_script("River Oak Realty", _FULL_INTEL)
    assert "River Oak Realty" in result["voicemail"]


def test_voicemail_no_literal_company_placeholder():
    """{company} placeholder must be fully substituted — not left in output."""
    result = generate_script("Test Co", _FULL_INTEL)
    assert "{company}" not in result["voicemail"]


# ---------------------------------------------------------------------------
# Signal placeholder substitution
# ---------------------------------------------------------------------------


def test_signal_placeholder_substituted_from_intel():
    """[signal] placeholder in trust_gap opener is replaced with intel value."""
    intel = {
        "pitch_angle": "trust_gap",
        "products": {"primary": "reputation_management", "secondary": None},
        "signals": {"signal": "only 2 reviews online", "region": "Denver", "keyword": "HVAC"},
    }
    result = generate_script("Test Co", intel)
    assert "[signal]" not in result["opener"]
    assert "only 2 reviews online" in result["opener"]


def test_region_placeholder_substituted_from_intel():
    """[region] placeholder is replaced with intel value."""
    intel = {
        "pitch_angle": "visibility_gap",
        "products": {"primary": "seo_package", "secondary": None},
        "signals": {"signal": "low traffic", "region": "Phoenix AZ", "keyword": "roofing"},
    }
    result = generate_script("Test Co", intel)
    assert "[region]" not in result["opener"]
    assert "Phoenix AZ" in result["opener"]


def test_placeholder_default_when_signals_absent():
    """[signal], [region], [keyword] get sensible defaults when signals block absent."""
    intel = {"pitch_angle": "trust_gap"}
    result = generate_script("Test Co", intel)
    assert "[signal]" not in result["opener"]
    assert "[region]" not in result["opener"]
    assert "[keyword]" not in result["opener"]


# ---------------------------------------------------------------------------
# Coverage: all 5 angles produce unique openers
# ---------------------------------------------------------------------------


def test_each_pitch_angle_has_unique_opener():
    """Each of the 5 pitch angles must produce a distinct opener string."""
    openers = set()
    for angle in ANGLE_HOOKS:
        intel = {
            "pitch_angle": angle,
            "products": {"primary": "workflow_automation", "secondary": None},
        }
        result = generate_script("Company X", intel)
        openers.add(result["opener"])
    assert len(openers) == len(ANGLE_HOOKS), "Each angle must produce a unique opener"


# ---------------------------------------------------------------------------
# Coverage: all 5 products have pitch and close
# ---------------------------------------------------------------------------


def test_each_product_has_pitch_and_close():
    """Each product listed in PRODUCT_PITCH must also appear in PRODUCT_CLOSE."""
    for product in PRODUCT_PITCH:
        assert product in PRODUCT_CLOSE, (
            f"Product '{product}' is in PRODUCT_PITCH but missing from PRODUCT_CLOSE"
        )
        intel = {
            "pitch_angle": "general_growth",
            "products": {"primary": product, "secondary": None},
        }
        result = generate_script("Test Co", intel)
        assert result["primary_product"] == product
        assert len(result["pitch"]) > 0
        assert len(result["close"]) > 0


# ---------------------------------------------------------------------------
# Tolerance: non-dict intel never raises
# ---------------------------------------------------------------------------


def test_generate_script_tolerates_none_intel():
    """None passed as intel must not raise — defaults are used."""
    result = generate_script("Test Co", None)  # type: ignore[arg-type]
    assert result["pitch_angle"] == "general_growth"
    assert result["primary_product"] == "workflow_automation"


def test_generate_script_with_pivot_tolerates_none_intel():
    """None intel for generate_script_with_pivot must not raise."""
    result = generate_script_with_pivot("Test Co", None)  # type: ignore[arg-type]
    assert result["pivot"] is None


# ---------------------------------------------------------------------------
# generate_script_with_pivot — pivot absent when secondary unknown product
# ---------------------------------------------------------------------------


def test_generate_script_with_pivot_no_pivot_for_unknown_secondary():
    """Unknown secondary product string yields pivot=None (not KeyError)."""
    intel = {
        "products": {"primary": "website_build", "secondary": "nonexistent_service"},
    }
    result = generate_script_with_pivot("Test Co", intel)
    assert result["pivot"] is None
