"""Unit tests for backend.growth.dispatch_policy.

Verifies the policy table structure, flag-gating (all default-OFF),
the is_live / is_enabled predicates, route_growth_action (flag-off -> None,
flag-on + injected handler -> result), and policy_summary shape.
"""

from __future__ import annotations


import pytest

from backend.growth.dispatch_policy import (
    GROWTH_DISPATCH_TABLE,
    GrowthDispatchEntry,
    get_entry,
    is_enabled,
    is_live,
    policy_summary,
    route_growth_action,
)


# All growth flags that the policy table references.
_ALL_FLAGS = {e.flag for e in GROWTH_DISPATCH_TABLE}

# All actions registered in the table.
_ALL_ACTIONS = {e.action for e in GROWTH_DISPATCH_TABLE}


# ---------------------------------------------------------------------------
# Table structure
# ---------------------------------------------------------------------------


def test_table_has_twelve_entries():
    """Canonical count: 3 seo + 4 outreach + 2 proof + 3 referral = 12."""
    assert len(GROWTH_DISPATCH_TABLE) == 12


def test_every_entry_has_non_empty_fields():
    for e in GROWTH_DISPATCH_TABLE:
        assert e.workcell, f"missing workcell on {e.action}"
        assert e.action, "entry has empty action"
        assert e.flag.startswith("SAMUS_GROWTH_"), (
            f"flag {e.flag!r} does not match SAMUS_GROWTH_* convention"
        )


def test_expected_actions_registered():
    expected = {
        # seo
        "geo_format",
        "aio_analyze",
        "aio_probe",
        # outreach
        "repurpose_blog_post",
        "plan_social_calendar",
        "dispatch_social_calendar",
        "plan_nurture",
        # crm/proof
        "generate_case_study",
        "build_proof_wall",
        # crm/referral
        "referral_code",
        "referral_record",
        "referral_qualify",
    }
    assert expected <= _ALL_ACTIONS


def test_workcell_assignments():
    seo_actions = {"geo_format", "aio_analyze", "aio_probe"}
    outreach_actions = {
        "repurpose_blog_post",
        "plan_social_calendar",
        "dispatch_social_calendar",
        "plan_nurture",
    }
    crm_actions = {
        "generate_case_study",
        "build_proof_wall",
        "referral_code",
        "referral_record",
        "referral_qualify",
    }
    by_action = {e.action: e for e in GROWTH_DISPATCH_TABLE}
    for a in seo_actions:
        assert by_action[a].workcell == "seo"
    for a in outreach_actions:
        assert by_action[a].workcell == "outreach"
    for a in crm_actions:
        assert by_action[a].workcell == "crm"


def test_dispatch_calendar_and_plan_nurture_are_dry_run():
    """These two have external-send surface; dry_run=True pins them."""
    by_action = {e.action: e for e in GROWTH_DISPATCH_TABLE}
    assert by_action["dispatch_social_calendar"].dry_run is True
    assert by_action["plan_nurture"].dry_run is True
    assert by_action["aio_probe"].dry_run is True


# ---------------------------------------------------------------------------
# Flag-gating: all default OFF
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_growth_flags(monkeypatch):
    """Ensure no growth flags leak in from the environment."""
    for flag in _ALL_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    # Reset settings cache so any reload picks up the cleared env.
    from backend.common.settings import reload_settings

    reload_settings()


def test_all_actions_disabled_by_default():
    for action in _ALL_ACTIONS:
        assert is_enabled(action) is False, f"{action} should be disabled by default"


def test_all_actions_not_live_by_default():
    for action in _ALL_ACTIONS:
        assert is_live(action) is False, f"{action} should not be live by default"


def test_unknown_action_disabled_and_not_live():
    assert is_enabled("totally_fake_action") is False
    assert is_live("totally_fake_action") is False


# ---------------------------------------------------------------------------
# Flag-gating: enabling a group flag
# ---------------------------------------------------------------------------


def test_seo_flag_enables_geo_format(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")
    assert is_enabled("geo_format") is True


def test_seo_flag_geo_format_becomes_live(monkeypatch):
    """geo_format has dry_run=False so is_live follows the flag."""
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")
    assert is_live("geo_format") is True


def test_seo_flag_aio_probe_enabled_but_not_live(monkeypatch):
    """aio_probe has dry_run=True -> enabled when flag on, but never live."""
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")
    assert is_enabled("aio_probe") is True
    assert is_live("aio_probe") is False


def test_social_flag_enables_outreach_actions(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_SOCIAL_ENABLED", "1")
    for action in (
        "repurpose_blog_post",
        "plan_social_calendar",
        "dispatch_social_calendar",
        "plan_nurture",
    ):
        assert is_enabled(action) is True, f"{action} should be enabled"


def test_proof_flag_enables_crm_proof_actions(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_PROOF_ENABLED", "yes")
    assert is_enabled("generate_case_study") is True
    assert is_enabled("build_proof_wall") is True


def test_referral_flag_enables_crm_referral_actions(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_REFERRAL_ENABLED", "on")
    for action in ("referral_code", "referral_record", "referral_qualify"):
        assert is_enabled(action) is True


def test_flag_case_insensitive_true_variants(monkeypatch):
    for variant in ("TRUE", "True", "1", "yes", "YES", "on", "ON", "y"):
        monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", variant)
        assert is_enabled("geo_format") is True, f"variant {variant!r} should enable"


def test_flag_false_variants(monkeypatch):
    for variant in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", variant)
        assert is_enabled("geo_format") is False, f"variant {variant!r} should disable"


def test_enabling_one_flag_does_not_cross_bleed(monkeypatch):
    """Enabling the SEO flag must not affect social/proof/referral."""
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")
    for action in ("repurpose_blog_post", "generate_case_study", "referral_code"):
        assert is_enabled(action) is False


# ---------------------------------------------------------------------------
# get_entry
# ---------------------------------------------------------------------------


def test_get_entry_returns_correct_entry():
    e = get_entry("geo_format")
    assert isinstance(e, GrowthDispatchEntry)
    assert e.action == "geo_format"
    assert e.workcell == "seo"


def test_get_entry_unknown_returns_none():
    assert get_entry("not_a_growth_action") is None


# ---------------------------------------------------------------------------
# route_growth_action
# ---------------------------------------------------------------------------


def test_route_returns_none_when_flag_off():
    result = route_growth_action("geo_format", {"drafts": {}, "keywords": []})
    assert result is None


def test_route_calls_injected_handler_when_flag_on(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")
    called: list[dict] = []

    def _fake_handler(payload: dict) -> dict:
        called.append(payload)
        return {"ok": True}

    result = route_growth_action(
        "geo_format",
        {"drafts": {"h1": "test"}, "keywords": ["kw"]},
        handler_map={"geo_format": _fake_handler},
    )
    assert result == {"ok": True}
    assert len(called) == 1


def test_route_returns_none_for_unknown_action():
    result = route_growth_action("unknown_growth_action", {})
    assert result is None


def test_route_handler_exception_returns_none_not_raise(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")

    def _exploding(payload: dict) -> dict:
        raise RuntimeError("deliberate failure")

    result = route_growth_action(
        "geo_format",
        {},
        handler_map={"geo_format": _exploding},
    )
    assert result is None


# ---------------------------------------------------------------------------
# policy_summary
# ---------------------------------------------------------------------------


def test_policy_summary_returns_list_of_dicts():
    summary = policy_summary()
    assert isinstance(summary, list)
    assert len(summary) == len(GROWTH_DISPATCH_TABLE)
    for row in summary:
        assert "workcell" in row
        assert "action" in row
        assert "flag" in row
        assert "enabled" in row
        assert "live" in row


def test_policy_summary_all_disabled_by_default():
    summary = policy_summary()
    for row in summary:
        assert row["enabled"] is False, f"{row['action']} shows enabled=True by default"
        assert row["live"] is False


def test_policy_summary_reflects_enabled_flag(monkeypatch):
    monkeypatch.setenv("SAMUS_GROWTH_SEO_ENABLED", "true")
    summary = policy_summary()
    seo_rows = [r for r in summary if r["flag"] == "SAMUS_GROWTH_SEO_ENABLED"]
    assert all(r["enabled"] is True for r in seo_rows)
    non_seo_rows = [r for r in summary if r["flag"] != "SAMUS_GROWTH_SEO_ENABLED"]
    assert all(r["enabled"] is False for r in non_seo_rows)
