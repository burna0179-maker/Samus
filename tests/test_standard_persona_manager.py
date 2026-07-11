"""Persona registry (Samus STANDARD)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.standard.persona import Persona, PersonaManager, PersonaNotFound


def _persona() -> Persona:
    return Persona(
        persona_id="test",
        display_name="Test",
        tagline="test persona",
        trim_color="#dc2626",
        default_bag={"preset_id": "samus_console", "spice_enabled": False},
    )


def test_empty_path_means_empty_registry(tmp_path: Path):
    assert PersonaManager(tmp_path / "personas.json").list() == []


def test_upsert_persists_and_round_trips(tmp_path: Path):
    path = tmp_path / "personas.json"
    mgr = PersonaManager(path)
    mgr.upsert(_persona())
    reloaded = PersonaManager(path)
    assert reloaded.ids() == ["test"]
    assert reloaded.get("test").display_name == "Test"


def test_get_unknown_raises(tmp_path: Path):
    with pytest.raises(PersonaNotFound):
        PersonaManager(tmp_path / "personas.json").get("nope")


def test_remove_persists_deletion(tmp_path: Path):
    path = tmp_path / "personas.json"
    mgr = PersonaManager(path)
    mgr.upsert(_persona())
    mgr.remove("test")
    assert PersonaManager(path).ids() == []


def test_persona_bag_returns_validated_chat_enrichment_bag():
    bag = _persona().bag()
    assert type(bag).__name__ == "ChatEnrichmentBag"
    assert bag.preset_id == "samus_console"


def test_persona_bag_falls_back_to_defaults_when_dict_is_bad():
    assert (
        type(
            Persona(persona_id="bad", display_name="X", default_bag={"spice_turns": -5}).bag()
        ).__name__
        == "ChatEnrichmentBag"
    )
