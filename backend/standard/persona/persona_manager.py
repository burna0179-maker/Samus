"""JSON-backed persona registry (Samus STANDARD).

Layout on disk::

    <data_root>/identity/personas/personas.json   # registry
    <data_root>/identity/personas/avatars/        # optional avatars

Default ``<data_root>`` is the value of ``SAMUS_DATA_ROOT`` (when set) or
``/opt/samus/data`` (matching the Samus runtime convention).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from backend.standard.chat._safe_io import atomic_write_text
from backend.standard.chat.enrichment_models import ChatEnrichmentBag

__tier__ = "STANDARD"


class PersonaNotFound(KeyError):
    pass


@dataclass(frozen=True)
class Persona:
    persona_id: str
    display_name: str
    tagline: str = ""
    trim_color: str = "#dc2626"
    avatar_path: str = ""
    default_bag: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Persona":
        return cls(
            persona_id=str(raw["persona_id"]),
            display_name=str(raw.get("display_name", raw["persona_id"])),
            tagline=str(raw.get("tagline", "")),
            trim_color=str(raw.get("trim_color", "#dc2626")),
            avatar_path=str(raw.get("avatar_path", "")),
            default_bag=dict(raw.get("default_bag", {})),
        )

    def bag(self) -> ChatEnrichmentBag:
        try:
            return ChatEnrichmentBag.model_validate(self.default_bag)
        except Exception:
            return ChatEnrichmentBag()


class PersonaManager:
    def __init__(self, path: Path | str) -> None:
        self._lock = RLock()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Persona] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            self._index = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._index = {}
            return
        items = raw.get("personas") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self._index = {}
            return
        new_index: dict[str, Persona] = {}
        for entry in items:
            if not isinstance(entry, dict) or "persona_id" not in entry:
                continue
            persona = Persona.from_dict(entry)
            new_index[persona.persona_id] = persona
        self._index = new_index

    def _flush(self) -> None:
        body = {
            "version": "1.0",
            "personas": [p.to_dict() for p in self._index.values()],
        }
        atomic_write_text(self._path, json.dumps(body, sort_keys=True, indent=2))

    def get(self, persona_id: str) -> Persona:
        with self._lock:
            persona = self._index.get(persona_id)
            if persona is None:
                raise PersonaNotFound(persona_id)
            return persona

    def list(self) -> list[Persona]:
        with self._lock:
            return list(self._index.values())

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._index.keys())

    def upsert(self, persona: Persona) -> None:
        with self._lock:
            self._index[persona.persona_id] = persona
            self._flush()

    def remove(self, persona_id: str) -> None:
        with self._lock:
            if persona_id not in self._index:
                raise PersonaNotFound(persona_id)
            del self._index[persona_id]
            self._flush()


def _default_data_root() -> Path:
    return Path(os.environ.get("SAMUS_DATA_ROOT", "/opt/samus/data"))


def load_persona_manager(root: Path | str | None = None) -> PersonaManager:
    base = Path(root) if root is not None else _default_data_root() / "identity"
    return PersonaManager(base / "personas" / "personas.json")


__all__ = ["Persona", "PersonaManager", "PersonaNotFound", "load_persona_manager"]
