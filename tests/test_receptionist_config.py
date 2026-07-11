"""AI Digital Receptionist — per-client config loader + resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.voice import receptionist_config as rc


def _write_config(root: Path, slug: str, body: str) -> None:
    cfg_dir = root / "customers" / slug / "receptionist"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def _artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    rc.clear_cache()
    # FIN-08: default the validator to "valid" so the existing config-loader
    # tests don't reach out to the real finance workcell. Tests that exercise
    # the gate override it explicitly.
    rc.set_stripe_customer_validator(lambda config, cid: (True, ""))
    yield tmp_path
    rc.clear_cache()
    rc.set_stripe_customer_validator(None)  # restore the default validator


def test_load_config_parses_yaml(_artifact_root):
    _write_config(
        _artifact_root,
        "acme_plumbing",
        """
business_name: Acme Plumbing
timezone: America/Los_Angeles
phone_numbers:
  - "+15305551212"
greeting: "Thanks for calling Acme Plumbing."
after_hours_behavior: voicemail
transfer_number: "+15305559999"
vapi_phone_number_id: pn_acme
stripe_customer_id: cus_acme
status: active
""",
    )
    cfg = rc.load_config("acme_plumbing")
    assert cfg is not None
    assert cfg.customer_slug == "acme_plumbing"  # stamped from dir name
    assert cfg.business_name == "Acme Plumbing"
    assert cfg.phone_numbers == ["+15305551212"]
    assert cfg.stripe_customer_id == "cus_acme"
    assert cfg.billing_sku_id == "retainer_ai_receptionist"  # default


def test_load_config_missing_returns_none(_artifact_root):
    assert rc.load_config("nobody") is None


def test_load_config_invalid_yaml_returns_none(_artifact_root):
    _write_config(_artifact_root, "broken", "business_name: [unterminated")
    assert rc.load_config("broken") is None


def test_load_config_non_mapping_returns_none(_artifact_root):
    _write_config(_artifact_root, "listy", "- just\n- a\n- list\n")
    assert rc.load_config("listy") is None


def test_resolve_customer_for_number(_artifact_root):
    _write_config(_artifact_root, "acme", 'phone_numbers: ["+15305551212"]\nstatus: active\n')
    rc.clear_cache()
    assert rc.resolve_customer_for_number("+15305551212").customer_slug == "acme"
    assert rc.resolve_customer_for_number("+19999999999") is None
    assert rc.resolve_customer_for_number("") is None


def test_resolve_customer_for_vapi_number(_artifact_root):
    _write_config(_artifact_root, "acme", "vapi_phone_number_id: pn_xyz\nstatus: active\n")
    rc.clear_cache()
    assert rc.resolve_customer_for_vapi_number("pn_xyz").customer_slug == "acme"
    assert rc.resolve_customer_for_vapi_number("pn_other") is None


def test_paused_client_excluded_from_resolution(_artifact_root):
    _write_config(_artifact_root, "paused_co", 'phone_numbers: ["+15305550000"]\nstatus: paused\n')
    rc.clear_cache()
    assert rc.resolve_customer_for_number("+15305550000") is None
    assert rc.list_active_configs() == []


# ---------------------------------------------------------------------------
# FIN-08 — stripe_customer_id validation gates metering (fail-closed)
# ---------------------------------------------------------------------------


def test_valid_stripe_customer_id_leaves_metering_enabled(_artifact_root):
    seen = []
    rc.set_stripe_customer_validator(
        lambda config, cid: seen.append(cid) or (True, ""),
    )
    _write_config(_artifact_root, "acme", "stripe_customer_id: cus_acme\nstatus: active\n")
    rc.clear_cache()
    cfg = rc.load_config("acme")
    assert cfg is not None
    assert cfg.metering_disabled is False
    assert cfg.metering_disabled_reason == ""
    assert seen == ["cus_acme"]  # validator was called exactly once


def test_invalid_stripe_customer_id_disables_metering(_artifact_root):
    rc.set_stripe_customer_validator(
        lambda config, cid: (False, "stripe_customer_not_found"),
    )
    _write_config(_artifact_root, "typo_co", "stripe_customer_id: cus_TYPO\nstatus: active\n")
    rc.clear_cache()
    cfg = rc.load_config("typo_co")
    assert cfg is not None
    assert cfg.metering_disabled is True
    assert cfg.metering_disabled_reason == "stripe_customer_not_found"


def test_validation_runs_once_per_load_cached_with_config(_artifact_root):
    calls = []
    rc.set_stripe_customer_validator(
        lambda config, cid: calls.append(cid) or (True, ""),
    )
    _write_config(
        _artifact_root,
        "acme",
        'phone_numbers: ["+15305551212"]\nstripe_customer_id: cus_acme\nstatus: active\n',
    )
    rc.clear_cache()
    # First resolution validates + caches; subsequent cached reads must NOT
    # re-validate (one validation per config-load, not per call).
    rc.resolve_customer_for_number("+15305551212")
    rc.resolve_customer_for_number("+15305551212")
    assert calls == ["cus_acme"]


def test_empty_stripe_customer_id_is_not_validated(_artifact_root):
    calls = []
    rc.set_stripe_customer_validator(
        lambda config, cid: calls.append(cid) or (True, ""),
    )
    _write_config(_artifact_root, "no_billing", "status: active\n")
    rc.clear_cache()
    cfg = rc.load_config("no_billing")
    assert cfg is not None
    assert cfg.metering_disabled is False
    assert calls == []  # empty id -> validator not called


def test_raising_validator_fails_closed(_artifact_root):
    def _boom(config, cid):
        raise RuntimeError("finance unreachable")

    rc.set_stripe_customer_validator(_boom)
    _write_config(_artifact_root, "acme", "stripe_customer_id: cus_acme\nstatus: active\n")
    rc.clear_cache()
    cfg = rc.load_config("acme")
    assert cfg is not None
    assert cfg.metering_disabled is True
    assert "validator_error" in cfg.metering_disabled_reason


def test_yaml_cannot_preclear_metering_disabled_flag(_artifact_root):
    # An operator (or attacker) hand-setting metering_disabled: false in the
    # YAML must NOT bypass the loader gate.
    rc.set_stripe_customer_validator(
        lambda config, cid: (False, "stripe_customer_not_found"),
    )
    _write_config(
        _artifact_root,
        "sneaky",
        "stripe_customer_id: cus_BAD\nmetering_disabled: false\nstatus: active\n",
    )
    rc.clear_cache()
    cfg = rc.load_config("sneaky")
    assert cfg is not None
    assert cfg.metering_disabled is True
