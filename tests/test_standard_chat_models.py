"""Pydantic models for the chat-enrichment variable system (Samus STANDARD)."""

from __future__ import annotations

import pytest

from backend.standard.chat import (
    ChatEnrichmentBag,
    EnrichmentContext,
    PromptPieceKind,
    PromptPieceLibrary,
    ScenarioPreset,
    SpicePool,
)
from backend.standard.chat.enrichment_models import SINGLE_SLOT_KINDS


def test_kind_enum_has_nine_canonical_slots():
    assert {k.value for k in PromptPieceKind} == {
        "character",
        "location",
        "relationship",
        "goals",
        "format",
        "scenario",
        "extras",
        "emotions",
        "spice",
    }


def test_single_slot_kinds_is_six():
    assert len(SINGLE_SLOT_KINDS) == 6


def test_library_get_missing_returns_none():
    lib = PromptPieceLibrary(pieces={PromptPieceKind.CHARACTER: {"core": "You are S."}})
    assert lib.get(PromptPieceKind.CHARACTER, "core") == "You are S."
    assert lib.get(PromptPieceKind.CHARACTER, "missing") is None


def test_scenario_preset_round_trip():
    p = ScenarioPreset(preset_id="samus_console", character="core")
    again = ScenarioPreset.model_validate(p.model_dump())
    assert again == p


def test_chat_enrichment_bag_defaults_match_samus_canon():
    bag = ChatEnrichmentBag()
    assert bag.preset_id == "samus_console"
    assert bag.spice_enabled is True
    assert bag.trim_color == "#dc2626"


def test_chat_enrichment_bag_validates_trim_color():
    with pytest.raises(Exception):
        ChatEnrichmentBag.model_validate({"trim_color": "nope"})


def test_enrichment_context_append_drops_empty_body():
    ctx = EnrichmentContext(chat_id="c1")
    ctx.append("label", "   ")
    ctx.append("evidence", "abc")
    assert ctx.parts == ["[evidence]\nabc"]


def test_spice_pool_get_unknown_returns_none():
    pool = SpicePool(categories={"default": ["a"]})
    assert pool.get("default").lines == ["a"]
    assert pool.get("nope") is None
