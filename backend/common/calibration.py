"""Closed-loop calibration store (Tranche 3 memory consolidation, CALIBRATE).

The nightly consolidator recomputes empirically-grounded values for constants
that today are hand-tuned:

  * CRM tier close-probabilities (``backend/crm/scoring.py``'s
    ``tier_close_probability`` baselines), and
  * ``backend/strategy/optimizer.py`` seed constants (default conversion
    probability).

It writes them here; consumers read them as **override-if-present** so a
missing/empty store means byte-identical legacy behaviour.

Integration note (file-ownership): ``crm/scoring.py`` and
``strategy/optimizer.py`` are owned by sibling branches this tranche must not
edit. The read-side helpers (:func:`calibrated_tier_close_probability`,
:func:`calibrated_optimizer_seed`) are fully built and tested here; wiring
them into ``scoring.py`` / ``optimizer.py`` is a two-line change per module
that lands at merge.

Store shape (one JSON document, atomic replace)::

    {"computed_at": "...", "day": "YYYY-MM-DD", "samples": {...},
     "tier_close_probability": {"low": 0.04, ...},
     "optimizer_seeds": {"conversion_prob_default": 0.12, ...}}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from backend.common.dates import iso_now
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.common.calibration")

ENV_CALIBRATION_PATH = "SAMUS_CALIBRATION_PATH"
_CALIBRATION_JSON = ("cognition", "calibration.json")


def calibration_path() -> Path:
    override = (os.getenv(ENV_CALIBRATION_PATH) or "").strip()
    if override:
        return Path(override)
    return state_path(*_CALIBRATION_JSON)


def read_calibration() -> dict[str, Any]:
    """The current calibration document; ``{}`` when absent/unreadable."""
    path = calibration_path()
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        _LOG.warning("calibration read failed: %s", exc)
        return {}


def write_calibration(
    *,
    tier_close_probability: dict[str, float] | None = None,
    optimizer_seeds: dict[str, float] | None = None,
    samples: dict[str, Any] | None = None,
    day: str = "",
) -> bool:
    """Atomically replace the calibration document. Fail-soft (bool)."""
    doc = {
        "computed_at": iso_now(),
        "day": day,
        "samples": dict(samples or {}),
        "tier_close_probability": {
            str(k): float(v) for k, v in (tier_close_probability or {}).items()
        },
        "optimizer_seeds": {str(k): float(v) for k, v in (optimizer_seeds or {}).items()},
    }
    path = calibration_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _LOG.warning("calibration write failed: %s", exc)
        return False


def _clamped_float(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def calibrated_tier_close_probability(tier: str, default: float) -> float:
    """Override-if-present read of one tier's close probability."""
    overrides = read_calibration().get("tier_close_probability") or {}
    if tier in overrides:
        return _clamped_float(overrides[tier], default)
    return default


def calibrated_optimizer_seed(name: str, default: float) -> float:
    """Override-if-present read of one optimizer seed constant."""
    seeds = read_calibration().get("optimizer_seeds") or {}
    if name in seeds:
        try:
            return float(seeds[name])
        except (TypeError, ValueError):
            return default
    return default


__all__ = [
    "ENV_CALIBRATION_PATH",
    "calibration_path",
    "read_calibration",
    "write_calibration",
    "calibrated_tier_close_probability",
    "calibrated_optimizer_seed",
]
