"""Heat-score math — deterministic email-reputation stress computation.

Each reputation signal is a rate over today's sends. A signal is mapped to a
[0,1] "danger" by dividing by the rate at which that signal ALONE is a
reputation emergency (e.g. ~0.4% spam complaints, ~12% hard bounces — the
thresholds Gmail/Outlook/SNDS actually punish), clamped to 1.0. The heat score
is a weighted combination of those dangers, clamped to [0,1].

Deliberately NOT a sum-to-1 weighting: spam complaints are the nuclear signal,
so the complaint danger is weighted to **solo-trip critical** — if effectively
every delivered email draws a complaint, the score must read critical even with
zero bounces. The other signals are contributory (they raise heat together with
complaints but rarely solo-trip).
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Per-signal "full danger" rates: the rate at which a signal ALONE is a
#    reputation emergency (danger -> 1.0). ───────────────────────────────────
COMPLAINT_FULL_DANGER_RATE: float = 0.004   # 0.4% spam complaints
BOUNCE_FULL_DANGER_RATE: float = 0.12       # 12% bounces
BLOCK_FULL_DANGER_RATE: float = 0.10        # 10% blocked/dropped
DEFERRAL_FULL_DANGER_RATE: float = 0.40     # 40% deferrals

# ── Contribution weights. Complaint >= 1.0 so it can solo-reach critical; the
#    rest are contributory. (They intentionally do NOT sum to 1.) ────────────
WEIGHT_COMPLAINT: float = 1.0
WEIGHT_BOUNCE: float = 0.6
WEIGHT_BLOCK: float = 0.5
WEIGHT_DEFERRAL: float = 0.25

HEAT_SCORE_MIN: float = 0.0
HEAT_SCORE_MAX: float = 1.0


def _danger(rate: float, full_danger_rate: float) -> float:
    """Map an events/sent rate to [0,1] danger against its emergency rate."""
    if rate <= 0.0 or full_danger_rate <= 0.0:
        return 0.0
    return min(1.0, rate / full_danger_rate)


@dataclass(frozen=True)
class HeatInputs:
    """Normalised reputation-stress signals (each a rate over today's sends).

    - ``complaint_rate`` — spam complaints / delivered.
    - ``bounce_rate``    — hard+soft bounces / sent.
    - ``block_rate``     — dropped/blocked / sent.
    - ``deferral_rate``  — deferred / sent.
    """

    complaint_rate: float = 0.0
    bounce_rate: float = 0.0
    block_rate: float = 0.0
    deferral_rate: float = 0.0


def compute_heat_score(inputs: HeatInputs) -> float:
    """Return the deterministic heat score for ``inputs`` in ``[0.0, 1.0]``.

    Pure + deterministic. A maxed complaint rate alone reaches the top of the
    scale (critical); the other signals add to it.
    """
    raw = (
        _danger(inputs.complaint_rate, COMPLAINT_FULL_DANGER_RATE) * WEIGHT_COMPLAINT
        + _danger(inputs.bounce_rate, BOUNCE_FULL_DANGER_RATE) * WEIGHT_BOUNCE
        + _danger(inputs.block_rate, BLOCK_FULL_DANGER_RATE) * WEIGHT_BLOCK
        + _danger(inputs.deferral_rate, DEFERRAL_FULL_DANGER_RATE) * WEIGHT_DEFERRAL
    )
    return max(HEAT_SCORE_MIN, min(HEAT_SCORE_MAX, raw))


__all__ = [
    "HeatInputs",
    "compute_heat_score",
    "COMPLAINT_FULL_DANGER_RATE",
    "BOUNCE_FULL_DANGER_RATE",
    "BLOCK_FULL_DANGER_RATE",
    "DEFERRAL_FULL_DANGER_RATE",
    "WEIGHT_COMPLAINT",
    "WEIGHT_BOUNCE",
    "WEIGHT_BLOCK",
    "WEIGHT_DEFERRAL",
    "HEAT_SCORE_MIN",
    "HEAT_SCORE_MAX",
]
