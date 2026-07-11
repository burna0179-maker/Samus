"""Spice rotation (Samus STANDARD)."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from .enrichment_models import SpicePool

__tier__ = "STANDARD"


@dataclass
class SpiceState:
    category_id: str = "default"
    index: int = 0
    turn: int = 0
    current: str = ""


@dataclass
class SpiceRotator:
    pool: SpicePool
    spice_turns: int = 3
    _lock: RLock = field(default_factory=RLock, repr=False)

    def __post_init__(self) -> None:
        if self.spice_turns < 1:
            raise ValueError("spice_turns must be >= 1")

    def next_spice(self, state: SpiceState) -> str:
        with self._lock:
            state.turn += 1
            should_rotate = state.turn == 1 or (state.turn - 1) % self.spice_turns == 0
            if not should_rotate and state.current:
                return state.current
            category = self.pool.get(state.category_id)
            if category is None or not category.lines:
                state.current = ""
                return ""
            state.index = state.index % len(category.lines) if state.current else 0
            line = category.lines[state.index]
            state.index = (state.index + 1) % len(category.lines)
            state.current = line
            return line

    def peek(self, state: SpiceState) -> str:
        return state.current


__all__ = ["SpiceRotator", "SpiceState"]
