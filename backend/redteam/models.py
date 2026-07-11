"""Samus-Red resilience models -- pure dataclasses, no I/O.

Samus-Red is the adversarial half of an executive self-play loop: a
deterministic sentinel that probes the system's *current defensive posture*
against known attack / failure modes and asks, per probe, "would this defense
hold?". Probes never mutate anything -- they READ posture and score
containment. The sentinel (see :mod:`backend.redteam.sentinel`) runs them
nightly, records the outcome to an append-only ledger, and files any BREACH
into the guidance ledger as an operator-owned recommendation (Blue's
remediation queue).

Antifragility is measured *across* runs: a breach that appeared in a prior run
and is CONTAINED in a later one is proof the system became harder to break.
See :attr:`ResilienceReport.antifragility_delta`.

This module is pure: dataclasses + scoring only. No filesystem, no network, no
settings (mirrors the ``guidance_models`` / ``cycle_models`` convention where
models live apart from the engine that operates on them).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List

# Ledger row discriminator (mirrors ``kind="guidance_record"``). The red-team
# ledger only reasons about rows it stamped.
KIND = "redteam_report"


class ProbeOutcome(str, Enum):
    """Per-probe verdict.

    CONTAINED  -- the defense held; the simulated attack would be caught/blocked.
    DEGRADED   -- a defense exists but is weakened or at-threshold (partial hold).
    BREACHED   -- the defense failed; the simulated attack would succeed.
    UNKNOWN    -- posture could not be sensed (fail-soft); excluded from scoring.
    """

    CONTAINED = "contained"
    DEGRADED = "degraded"
    BREACHED = "breached"
    UNKNOWN = "unknown"


# Severity -> scoring weight. Lower severity number == more critical, so a
# critical probe contributes more to the resilience score than a moderate one.
# weight = 4 - severity : critical(1)->3, high(2)->2, moderate(3)->1.
_SEVERITY_MIN = 1
_SEVERITY_MAX = 3

# DEGRADED counts as a partial hold when scoring.
_DEGRADED_CREDIT = 0.5


def severity_weight(severity: int) -> int:
    """Scoring weight for a severity band (critical weighs most)."""
    s = max(_SEVERITY_MIN, min(_SEVERITY_MAX, int(severity)))
    return (_SEVERITY_MAX + 1) - s


@dataclass(frozen=True)
class ProbeResult:
    """One deterministic attack probe's outcome against current posture."""

    probe: str            # stable probe id (e.g. "immutable_integrity")
    outcome: str          # ProbeOutcome value
    severity: int         # 1=critical, 2=high, 3=moderate
    title: str            # human-readable description of the attack
    evidence: str = ""    # what was actually observed
    remediation: str = "" # what Blue should do to contain it
    owner: str = "operator"  # who fixes it (guidance routing)

    @property
    def breached(self) -> bool:
        return self.outcome == ProbeOutcome.BREACHED.value

    @property
    def scorable(self) -> bool:
        return self.outcome != ProbeOutcome.UNKNOWN.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "outcome": self.outcome,
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ProbeResult":
        return cls(
            probe=str(row.get("probe", "")),
            outcome=str(row.get("outcome", ProbeOutcome.UNKNOWN.value)),
            severity=int(row.get("severity", _SEVERITY_MAX) or _SEVERITY_MAX),
            title=str(row.get("title", "")),
            evidence=str(row.get("evidence", "") or ""),
            remediation=str(row.get("remediation", "") or ""),
            owner=str(row.get("owner", "operator") or "operator"),
        )


@dataclass
class ResilienceReport:
    """The nightly Samus-Red scorecard for one day."""

    day: str
    ts: str
    results: List[ProbeResult] = field(default_factory=list)
    resilience_score: float = 0.0        # 0.0 .. 1.0 (weighted contained fraction)
    breaches: List[str] = field(default_factory=list)          # breached probe ids
    prior_breaches: List[str] = field(default_factory=list)    # breached ids last run
    hardened: List[str] = field(default_factory=list)          # prior breach now contained
    regressed: List[str] = field(default_factory=list)         # newly breached this run
    antifragility_delta: int = 0         # len(hardened) - len(regressed)
    kind: str = KIND

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "day": self.day,
            "ts": self.ts,
            "resilience_score": self.resilience_score,
            "breaches": list(self.breaches),
            "prior_breaches": list(self.prior_breaches),
            "hardened": list(self.hardened),
            "regressed": list(self.regressed),
            "antifragility_delta": self.antifragility_delta,
            "results": [r.to_dict() for r in self.results],
        }


def compute_resilience_score(results: List[ProbeResult]) -> float:
    """Severity-weighted fraction of defenses that held.

    CONTAINED contributes full weight, DEGRADED half, BREACHED zero. UNKNOWN
    probes are excluded from the denominator (posture could not be judged). A
    run with no scorable probes scores 0.0 (nothing was verified).
    """
    numerator = 0.0
    denominator = 0.0
    for r in results:
        if not r.scorable:
            continue
        w = severity_weight(r.severity)
        denominator += w
        if r.outcome == ProbeOutcome.CONTAINED.value:
            numerator += w
        elif r.outcome == ProbeOutcome.DEGRADED.value:
            numerator += w * _DEGRADED_CREDIT
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def build_report(
    day: str,
    ts: str,
    results: List[ProbeResult],
    prior_breaches: List[str] | None = None,
) -> ResilienceReport:
    """Assemble a :class:`ResilienceReport` and compute antifragility vs the last run."""
    prior = list(prior_breaches or [])
    prior_set = set(prior)
    breaches = [r.probe for r in results if r.breached]
    breach_set = set(breaches)
    contained_now = {
        r.probe for r in results if r.outcome == ProbeOutcome.CONTAINED.value
    }
    hardened = sorted(prior_set & contained_now)   # were broken, now held
    regressed = sorted(breach_set - prior_set)     # newly broken this run
    return ResilienceReport(
        day=day,
        ts=ts,
        results=list(results),
        resilience_score=compute_resilience_score(results),
        breaches=breaches,
        prior_breaches=prior,
        hardened=hardened,
        regressed=regressed,
        antifragility_delta=len(hardened) - len(regressed),
    )


__all__ = [
    "KIND",
    "ProbeOutcome",
    "ProbeResult",
    "ResilienceReport",
    "severity_weight",
    "compute_resilience_score",
    "build_report",
]
