"""Autonomy subsystems — the six META-PERCEPTION / META-REFLECTION collaborators.

These are the six ``backend.autonomy.*`` classes the ``MetaCognitionEngine``
(``recovery/meta_cognition_engine.py``, ported natively in Phase D) wraps
around the hardened ``CognitiveLoop``:

  * ``SituationIndex``               — META-PERCEPTION situation classifier.
  * ``HeuristicRegistry``            — META-PERCEPTION advisory pre-bias text.
  * ``ReinforcementHeuristicEngine`` — bounded RL weight evolution (flag-gated,
                                       persistent under state_path).
  * ``Autotuner``                    — advisory runtime self-tuning (flag-gated).
  * ``UpgradeEngine``                — propose-ONLY architecture upgrades
                                       (flag-gated; execute() writes a proposal
                                       record, NEVER self-modifies code).
  * ``ArchitecturePersistence``      — durable per-cycle audit snapshot.

Each class constructs no-arg and is fail-safe (Contract 2). The three flagged
subsystems default OFF and are inert until armed
(``autonomy_reinforcement_enabled`` / ``autonomy_autotuner_enabled`` /
``autonomy_upgrade_enabled``).

DORMANCY: this is a THIRD, distinct autonomy construct — separate from
``backend/common/autonomy.py`` (MAPE-K planner) and
``backend/identity/autonomy_layer.py`` (``_shared.autonomy`` wiring). It has
NO live importer; the live Samus runtime never imports this package.
"""
from __future__ import annotations

from .architecture_persistence import ArchitecturePersistence
from .autotuner import Autotuner
from .heuristic_registry import HeuristicRegistry
from .reinforcement_heuristics import HeuristicProfile, ReinforcementHeuristicEngine
from .situation_index import SituationIndex
from .upgrade_engine import UpgradeEngine

__all__ = [
    "SituationIndex",
    "HeuristicRegistry",
    "ReinforcementHeuristicEngine",
    "HeuristicProfile",
    "Autotuner",
    "UpgradeEngine",
    "ArchitecturePersistence",
]
