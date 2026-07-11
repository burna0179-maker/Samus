"""PromptAssembler (Samus STANDARD)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .enrichment_models import (
    ChatEnrichmentBag, EnrichmentContext, PromptPieceKind,
    PromptPieceLibrary, ScenarioPreset, SpicePool,
)
from .enrichment_resolver import EnrichmentResolver
from .spice_rotator import SpiceRotator, SpiceState

__tier__ = "STANDARD"

_SECTION_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class AssembledPrompt:
    text: str
    sections: tuple[tuple[str, str], ...]
    char_count: int


@dataclass
class PromptAssembler:
    library: PromptPieceLibrary
    pool: SpicePool
    resolver: EnrichmentResolver = field(default_factory=EnrichmentResolver)

    def _piece(self, kind, slug, variables):
        if not slug:
            return ("", "")
        text = self.library.get(kind, slug) or ""
        if not text:
            return (kind.value, "")
        return (kind.value, self.resolver.render(text, variables))

    def _multi(self, kind, slugs, variables):
        out = []
        for slug in slugs:
            label, text = self._piece(kind, slug, variables)
            if text:
                out.append((f"{label}:{slug}", text))
        return out

    def assemble(self, preset, bag, context, *, variables=None, spice_state=None):
        vars_ = variables or {}
        sections: list[tuple[str, str]] = []
        for kind in (
            PromptPieceKind.CHARACTER, PromptPieceKind.LOCATION,
            PromptPieceKind.RELATIONSHIP, PromptPieceKind.GOALS,
            PromptPieceKind.FORMAT, PromptPieceKind.SCENARIO,
        ):
            slug = getattr(preset, kind.value)
            label, text = self._piece(kind, slug, vars_)
            if text:
                sections.append((label, text))

        sections.extend(self._multi(PromptPieceKind.EXTRAS, preset.extras, vars_))
        sections.extend(self._multi(PromptPieceKind.EMOTIONS, preset.emotions, vars_))

        if bag.inject_datetime:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sections.append(("datetime", f"Current UTC time: {now}"))

        if bag.custom_context.strip():
            cleaned = self.resolver.sanitise(bag.custom_context.strip())
            if cleaned:
                sections.append(("custom_context", cleaned))

        if bag.inject_evidence_tip:
            tip = context.metadata.get("evidence_last_hash")
            if isinstance(tip, str) and tip:
                sections.append(("evidence_tip", f"Last evidence hash: {tip}"))

        for part in context.parts:
            cleaned = self.resolver.sanitise(part)
            if cleaned:
                sections.append(("context", cleaned))

        if bag.spice_enabled:
            rotator = SpiceRotator(pool=self.pool, spice_turns=max(1, bag.spice_turns))
            state = spice_state or SpiceState(category_id=preset.spice_category)
            spice = rotator.next_spice(state)
            if spice:
                sections.append(("spice", self.resolver.render(spice, vars_)))

        text = _SECTION_SEPARATOR.join(t for _, t in sections if t).strip()
        return AssembledPrompt(text=text, sections=tuple(sections), char_count=len(text))


__all__ = ["AssembledPrompt", "PromptAssembler"]
