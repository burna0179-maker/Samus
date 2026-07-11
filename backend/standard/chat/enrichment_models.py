"""Pydantic v2 models for the chat-enrichment variable system (Samus STANDARD)."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

__tier__ = "STANDARD"


class PromptPieceKind(str, Enum):
    CHARACTER = "character"
    LOCATION = "location"
    RELATIONSHIP = "relationship"
    GOALS = "goals"
    FORMAT = "format"
    SCENARIO = "scenario"
    EXTRAS = "extras"
    EMOTIONS = "emotions"
    SPICE = "spice"


_SINGLE_SLOT_KINDS: frozenset[PromptPieceKind] = frozenset(
    {
        PromptPieceKind.CHARACTER,
        PromptPieceKind.LOCATION,
        PromptPieceKind.RELATIONSHIP,
        PromptPieceKind.GOALS,
        PromptPieceKind.FORMAT,
        PromptPieceKind.SCENARIO,
    }
)


class PromptPiece(BaseModel):
    kind: PromptPieceKind
    slug: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)

    @field_validator("slug")
    @classmethod
    def _slug_no_whitespace(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("slug must not contain whitespace")
        return v


class PromptPieceLibrary(BaseModel):
    pieces: dict[PromptPieceKind, dict[str, str]] = Field(default_factory=dict)

    def get(self, kind: PromptPieceKind, slug: str) -> str | None:
        bucket = self.pieces.get(kind)
        return bucket.get(slug) if bucket else None

    def list_kind(self, kind: PromptPieceKind) -> list[PromptPiece]:
        return [
            PromptPiece(kind=kind, slug=slug, text=text)
            for slug, text in (self.pieces.get(kind) or {}).items()
        ]

    def upsert(self, piece: PromptPiece) -> None:
        bucket = self.pieces.setdefault(piece.kind, {})
        bucket[piece.slug] = piece.text


class ScenarioPreset(BaseModel):
    preset_id: str = Field(..., min_length=1)
    description: str = ""
    character: str = ""
    location: str = ""
    relationship: str = ""
    goals: str = ""
    format: str = ""
    scenario: str = ""
    extras: list[str] = Field(default_factory=list)
    emotions: list[str] = Field(default_factory=list)
    spice_category: str = "default"


class SpiceCategory(BaseModel):
    category_id: str = Field(..., min_length=1)
    lines: list[str] = Field(default_factory=list)


class SpicePool(BaseModel):
    categories: dict[str, list[str]] = Field(default_factory=dict)

    def get(self, category_id: str) -> SpiceCategory | None:
        lines = self.categories.get(category_id)
        if lines is None:
            return None
        return SpiceCategory(category_id=category_id, lines=list(lines))


class ChatEnrichmentBag(BaseModel):
    """Samus-flavoured defaults: red trim signals live production."""

    preset_id: str = Field(default="samus_console")
    scope_memory: str = Field(default="default")
    scope_goal: str = Field(default="default")
    scope_knowledge: str = Field(default="default")
    scope_people: str = Field(default="default")

    inject_datetime: bool = False
    inject_evidence_tip: bool = False
    custom_context: str = ""
    spice_enabled: bool = True
    spice_turns: int = Field(default=3, ge=1)

    trim_color: str = Field(default="#dc2626", pattern=r"^#[0-9A-Fa-f]{6}$")

    llm_provider: str | None = None
    llm_model: str | None = None
    private_chat: bool = False


class EnrichmentContext(BaseModel):
    chat_id: str
    turn: int = 0
    parts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    stop_propagation: bool = False

    def append(self, label: str, body: str) -> None:
        body = (body or "").strip()
        if not body:
            return
        self.parts.append(f"[{label}]\n{body}" if label else body)


SINGLE_SLOT_KINDS = _SINGLE_SLOT_KINDS

__all__ = [
    "ChatEnrichmentBag", "EnrichmentContext", "PromptPiece", "PromptPieceKind",
    "PromptPieceLibrary", "SINGLE_SLOT_KINDS", "ScenarioPreset",
    "SpiceCategory", "SpicePool",
]
