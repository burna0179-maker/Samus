"""Samus-Red -- deterministic adversarial resilience sentinel.

The adversarial half of an executive self-play loop. Each night Samus-Red
attacks its own defenses with a battery of deterministic probes (integrity
tamper, ethical-gate manipulation, operator-absence continuity), scores how
many held, measures antifragility against the previous run, and files any
breach into the guidance ledger as Blue's remediation work.

Public entry point: :func:`backend.redteam.sentinel.run_redteam_pass`.
"""
from __future__ import annotations

from .models import ProbeOutcome, ProbeResult, ResilienceReport
from .probes import DefensivePosture, run_probes
from .sensors import sense_posture
from .sentinel import run_redteam_pass

__all__ = [
    "run_redteam_pass",
    "sense_posture",
    "run_probes",
    "DefensivePosture",
    "ProbeOutcome",
    "ProbeResult",
    "ResilienceReport",
]
