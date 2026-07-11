"""JSON catalogue loaders (Samus STANDARD)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._safe_io import atomic_write_text
from .enrichment_models import (
    PromptPieceKind,
    PromptPieceLibrary,
    ScenarioPreset,
    SpicePool,
)

__tier__ = "STANDARD"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_prompt_pieces(path: Path | str) -> PromptPieceLibrary:
    raw = _read_json(Path(path))
    bucket = raw.get("pieces") if isinstance(raw, dict) else None
    if not isinstance(bucket, dict):
        return PromptPieceLibrary()
    normalised: dict[PromptPieceKind, dict[str, str]] = {}
    for kind_str, items in bucket.items():
        try:
            kind = PromptPieceKind(kind_str)
        except ValueError:
            continue
        if not isinstance(items, dict):
            continue
        normalised[kind] = {
            str(slug): str(text) for slug, text in items.items() if isinstance(text, str)
        }
    return PromptPieceLibrary(pieces=normalised)


def save_prompt_pieces(path: Path | str, library: PromptPieceLibrary) -> None:
    body = {
        "version": "1.0",
        "pieces": {kind.value: dict(items) for kind, items in library.pieces.items()},
    }
    atomic_write_text(Path(path), json.dumps(body, sort_keys=True, indent=2))


def load_scenario_presets(path: Path | str) -> dict[str, ScenarioPreset]:
    raw = _read_json(Path(path))
    presets = raw.get("presets") if isinstance(raw, dict) else None
    if not isinstance(presets, list):
        return {}
    out: dict[str, ScenarioPreset] = {}
    for entry in presets:
        if not isinstance(entry, dict) or "preset_id" not in entry:
            continue
        try:
            preset = ScenarioPreset.model_validate(entry)
        except Exception:
            continue
        out[preset.preset_id] = preset
    return out


def save_scenario_presets(path: Path | str, presets: dict[str, ScenarioPreset]) -> None:
    body = {
        "version": "1.0",
        "presets": [p.model_dump() for p in presets.values()],
    }
    atomic_write_text(Path(path), json.dumps(body, sort_keys=True, indent=2))


def load_spice_pool(path: Path | str) -> SpicePool:
    raw = _read_json(Path(path))
    categories = raw.get("categories") if isinstance(raw, dict) else None
    if not isinstance(categories, dict):
        return SpicePool()
    normalised: dict[str, list[str]] = {}
    for cat_id, lines in categories.items():
        if not isinstance(lines, list):
            continue
        normalised[str(cat_id)] = [str(line) for line in lines if isinstance(line, str)]
    return SpicePool(categories=normalised)


def save_spice_pool(path: Path | str, pool: SpicePool) -> None:
    body = {
        "version": "1.0",
        "categories": {cid: list(lines) for cid, lines in pool.categories.items()},
    }
    atomic_write_text(Path(path), json.dumps(body, sort_keys=True, indent=2))


class EnrichmentCatalogue:
    def __init__(self, *, library, presets, pool, root=None):
        self.library = library
        self.presets = presets
        self.pool = pool
        self.root = root

    @classmethod
    def load(cls, root: Path | str) -> "EnrichmentCatalogue":
        base = Path(root)
        return cls(
            library=load_prompt_pieces(base / "prompt_pieces.json"),
            presets=load_scenario_presets(base / "scenario_presets.json"),
            pool=load_spice_pool(base / "prompt_spices.json"),
            root=base,
        )

    def preset(self, preset_id: str) -> ScenarioPreset | None:
        return self.presets.get(preset_id)


__all__ = [
    "EnrichmentCatalogue",
    "load_prompt_pieces",
    "load_scenario_presets",
    "load_spice_pool",
    "save_prompt_pieces",
    "save_scenario_presets",
    "save_spice_pool",
]
