"""STANDARD chat enrichment primitives for Samus.

Pattern lifted in shape (not in source) from Sapphire v2.4.0; re-implemented
from scratch -- Sapphire is AGPL-3.0 and its source must not be copied.

Samus port of the Optimus STANDARD chat plane.
"""

from __future__ import annotations

__tier__ = "STANDARD"

from .catalogues import (
    EnrichmentCatalogue,
    load_prompt_pieces,
    load_scenario_presets,
    load_spice_pool,
    save_prompt_pieces,
    save_scenario_presets,
    save_spice_pool,
)
from .enrichment_models import (
    ChatEnrichmentBag,
    EnrichmentContext,
    PromptPiece,
    PromptPieceLibrary,
    PromptPieceKind,
    ScenarioPreset,
    SpiceCategory,
    SpicePool,
)
from .enrichment_resolver import EnrichmentResolver, TokenSubstitutionError
from .prompt_assembly import AssembledPrompt, PromptAssembler
from .spice_rotator import SpiceRotator, SpiceState

__all__ = [
    "AssembledPrompt",
    "ChatEnrichmentBag",
    "EnrichmentCatalogue",
    "EnrichmentContext",
    "EnrichmentResolver",
    "PromptAssembler",
    "PromptPiece",
    "PromptPieceKind",
    "PromptPieceLibrary",
    "ScenarioPreset",
    "SpiceCategory",
    "SpicePool",
    "SpiceRotator",
    "SpiceState",
    "TokenSubstitutionError",
    "load_prompt_pieces",
    "load_scenario_presets",
    "load_spice_pool",
    "save_prompt_pieces",
    "save_scenario_presets",
    "save_spice_pool",
]
