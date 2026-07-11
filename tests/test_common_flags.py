"""Tests for the Samus shared-core flag/config subsystem (E5 D13).

Covers: catalog registration, default-OFF dormant convention, is_enabled
resolution + runtime override, fail-closed behaviour when the store is not
initialised, persistence round-trip, type/shape guards, and env-binding
PARITY between the catalog defaults and the pydantic Settings field
defaults (the single-source-of-truth contract).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common import flags
from backend.common.flags import (
    FlagSpec,
    FlagStore,
    FlagSpecConflictError,
    FlagTypeMismatchError,
    FlagNotFoundError,
    FlagImmutableError,
    build_specs,
)
from backend.common.flags import runtime as flags_runtime


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> FlagStore:
    return FlagStore(persist_path=tmp_path / "flags" / "v.json")


@pytest.fixture
def default_store(tmp_path: Path):
    """Install a process-global store + register the full catalog; tear down."""
    flags.init_default_store(tmp_path / "flags" / "global.json")
    flags.register_all()
    yield
    flags.reset_default_store_for_testing()


# ---------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------


def test_catalog_registers_all_specs(default_store):
    specs = build_specs()
    assert len(specs) >= 40, "catalog should cover Samus's many feature gates"
    names = {fv.name for fv in flags.all_flags()}
    for spec in specs:
        assert spec.name in names


def test_register_is_idempotent_on_identical_shape(store):
    spec = FlagSpec(name="x", description="d", default_value=False, kind="bool")
    store.register(spec)
    # second register of an identical shape is a no-op, not an error
    store.register(spec)
    assert store.get("x").value is False


def test_register_conflicting_shape_raises(store):
    store.register(FlagSpec(name="x", description="d", default_value=False, kind="bool"))
    with pytest.raises(FlagSpecConflictError):
        store.register(
            FlagSpec(name="x", description="DIFFERENT", default_value=False, kind="bool")
        )


def test_register_type_mismatch_default_raises(store):
    with pytest.raises(FlagTypeMismatchError):
        store.register(
            FlagSpec(name="x", description="d", default_value="nope", kind="bool")
        )


# ---------------------------------------------------------------------
# Default-OFF dormant convention
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "website_live_publish_enabled",
        "website_media_gen_enabled",
        "website_media_video_enabled",
        "social_reel_enabled",
        "commerce_medusa_enabled",
        "tiktok_shop_enabled",
        "workflow_n8n_deploy_enabled",
        "vector_store_enabled",
        "samus_quorum_publish_enabled",
        "samus_quorum_voting_enabled",
        "taste_audit_enabled",
        "outreach_voice_send_enabled",
    ],
)
def test_dormant_flags_default_off(default_store, name):
    fv = flags.get_flag(name)
    assert fv.value is False, f"{name} must be dormant (default OFF)"
    assert fv.is_default is True


def test_efh_semantic_flag_default_on(default_store):
    """Armed HOTL Tranche 1 (2026-07-05): semantic EFH layer is ON by default."""
    fv = flags.get_flag("samus_efh_semantic_enabled")
    assert fv.value is True
    assert fv.is_default is True


def test_get_returns_default_when_unset(store):
    store.register(FlagSpec(name="x", description="d", default_value=True, kind="bool"))
    fv = store.get("x")
    assert fv.value is True
    assert fv.is_default is True
    assert fv.set_by == "default"


# ---------------------------------------------------------------------
# is_enabled + runtime override
# ---------------------------------------------------------------------


def test_is_enabled_reads_default(default_store):
    # cash_engine_enabled defaults ON in the model
    assert flags.is_enabled("cash_engine_enabled", settings_fallback=True) is True
    # a dormant flag is OFF
    assert flags.is_enabled("social_reel_enabled", settings_fallback=False) is False


def test_runtime_override_wins_over_settings(default_store):
    # operator flips a dormant flag ON at runtime; the flag value wins
    flags.set_flag("social_reel_enabled", True, actor="operator")
    assert flags.is_enabled("social_reel_enabled", settings_fallback=False) is True


def test_effective_value_falls_back_when_store_uninitialised():
    flags.reset_default_store_for_testing()
    # no store installed -> settings_fallback is returned verbatim
    assert flags_runtime.effective_value("anything", "fallback") == "fallback"
    assert flags_runtime.is_enabled("anything", True) is True
    assert flags_runtime.is_enabled("anything", False) is False


def test_is_enabled_fc_defaults_closed():
    flags.reset_default_store_for_testing()
    # fail-closed helper: unresolved -> False
    assert flags_runtime.is_enabled_fc("anything") is False


def test_effective_value_uses_flag_after_override(default_store):
    flags.set_flag("website_live_publish_enabled", True, actor="op")
    # even with a False settings fallback, the persisted override wins
    assert flags_runtime.effective_value("website_live_publish_enabled", False) is True


# ---------------------------------------------------------------------
# Writes: type guard, immutability, persistence round-trip
# ---------------------------------------------------------------------


def test_set_type_mismatch_raises(store):
    store.register(FlagSpec(name="x", description="d", default_value=False, kind="bool"))
    with pytest.raises(FlagTypeMismatchError):
        store.set("x", "true", actor="op")


def test_set_unregistered_raises(store):
    with pytest.raises(FlagNotFoundError):
        store.set("nope", True, actor="op")


def test_immutable_flag_pins_after_first_set(store):
    store.register(
        FlagSpec(name="x", description="d", default_value=False, kind="bool", mutable=False)
    )
    store.set("x", True, actor="op")
    with pytest.raises(FlagImmutableError):
        store.set("x", False, actor="op")
    # reset is still allowed (operator intent)
    fv = store.reset("x", actor="op")
    assert fv.value is False and fv.is_default is True


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "flags" / "v.json"
    s1 = FlagStore(persist_path=path)
    s1.register(FlagSpec(name="x", description="d", default_value=False, kind="bool"))
    s1.set("x", True, actor="op")
    # a fresh store over the same file re-loads the persisted value
    s2 = FlagStore(persist_path=path)
    s2.register(FlagSpec(name="x", description="d", default_value=False, kind="bool"))
    fv = s2.get("x")
    assert fv.value is True
    assert fv.is_default is False
    assert fv.set_by == "op"


def test_corrupt_persisted_type_falls_back_to_default(store, tmp_path):
    # a hand-edited / schema-drifted value of the wrong type is rejected in
    # favour of the declared default (fail closed to the known spec)
    path = tmp_path / "flags" / "v.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"version":1,"values":{"x":{"value":"WRONG","set_by":"x","set_at_ts":0}}}',
        encoding="utf-8",
    )
    s = FlagStore(persist_path=path)
    s.register(FlagSpec(name="x", description="d", default_value=True, kind="bool"))
    fv = s.get("x")
    assert fv.value is True
    assert fv.is_default is True


# ---------------------------------------------------------------------
# Env-binding parity: catalog defaults == Settings field defaults
# ---------------------------------------------------------------------


def test_catalog_defaults_match_settings_field_defaults():
    """Every flag's default_value equals the same-named Settings field's
    model default. This is the single-source-of-truth contract: the catalog
    can never silently diverge from the pydantic field default."""
    from backend.common.config import Settings

    s = Settings()
    for spec in build_specs():
        assert hasattr(s, spec.name), f"flag {spec.name} has no Settings field"
        field_default = getattr(s, spec.name)
        assert spec.default_value == field_default, (
            f"flag {spec.name} default {spec.default_value!r} != "
            f"Settings field default {field_default!r}"
        )


def test_catalog_env_var_bindings_are_set():
    """Every flag records the SAMUS_* env var it mirrors (operator surface)."""
    for spec in build_specs():
        assert spec.env_var, f"flag {spec.name} is missing its env_var binding"


def test_catalog_kinds_match_settings_field_types():
    """The declared FlagSpec.kind matches the actual Settings field value type."""
    from backend.common.config import Settings

    s = Settings()
    kind_of = {bool: "bool", int: "int", float: "float", str: "str"}
    for spec in build_specs():
        val = getattr(s, spec.name)
        # bool is an int subclass; check bool first
        if isinstance(val, bool):
            actual = "bool"
        else:
            actual = kind_of.get(type(val))
        assert actual == spec.kind, (
            f"flag {spec.name}: declared kind {spec.kind!r} != "
            f"settings field type {actual!r}"
        )
