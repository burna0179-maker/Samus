"""Samus-Red probes -- pure attack logic over a sensed defensive posture.

Each probe is a pure function ``(DefensivePosture) -> ProbeResult``. It reads
already-sensed facts (gathered fail-soft by :mod:`backend.redteam.sensors`) and
decides whether the defense would hold. A probe NEVER performs I/O -- that keeps
the attack logic deterministic and unit-testable by handing it a synthetic
posture. When the fact a probe needs was not sensed (``None``), the probe
returns ``UNKNOWN`` and is excluded from scoring rather than guessed.

The live registry (:data:`LIVE_PROBES`) holds only probes whose posture is
gathered by real sensors -- no placeholder probes that never fire.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from .models import ProbeOutcome, ProbeResult


@dataclass(frozen=True)
class DefensivePosture:
    """Sensed facts about current defenses. ``None`` == not sensed (UNKNOWN).

    All fields are optional so the sentinel can hand probes a partial posture
    when a sensor fails, and tests can construct exactly the slice they need.
    """

    # Immutable integrity gate (backend.identity.immutable_manifest.verify_manifest)
    immutable_baseline_recorded: Optional[bool] = None
    immutable_drifted_files: Optional[List[str]] = None

    # Governance fail-closed floor: axiom ids the EFH heuristic layer flags for a
    # deliberately manipulative synthetic payload. A healthy gate flags >= 1.
    governance_floor_installed: Optional[bool] = None
    governance_synthetic_attack_breaches: Optional[List[str]] = None

    # Autonomous business continuity if the operator disappears.
    continuity_master_loop_enabled: Optional[bool] = None      # cognitive_loop_enabled
    continuity_cadence_enabled: Optional[bool] = None          # cognition_cadence_enabled
    continuity_nightly_consolidation_enabled: Optional[bool] = None


def probe_immutable_integrity(p: DefensivePosture) -> ProbeResult:
    """Attack: tamper with a protected core file. Does the signed gate catch it?"""
    probe, sev, title = (
        "immutable_integrity", 1,
        "Tamper with a protected core file (Ed25519 immutable baseline)",
    )
    owner, remediation = (
        "operator",
        "Investigate the drifted file(s); if the change is legitimate, re-sign "
        "the immutable baseline (record_baseline + operator Ed25519 signature). "
        "If not, treat as intrusion.",
    )
    if p.immutable_baseline_recorded is None or p.immutable_drifted_files is None:
        return ProbeResult(probe, ProbeOutcome.UNKNOWN.value, sev, title,
                           evidence="immutable manifest posture unavailable",
                           remediation=remediation, owner=owner)
    if p.immutable_baseline_recorded is False:
        return ProbeResult(
            probe, ProbeOutcome.DEGRADED.value, sev, title,
            evidence="no immutable baseline recorded yet -- tamper cannot be detected",
            remediation="Seed and sign the immutable baseline so tamper is catchable.",
            owner=owner,
        )
    if p.immutable_drifted_files:
        return ProbeResult(
            probe, ProbeOutcome.BREACHED.value, sev, title,
            evidence="protected core files drifted from signed baseline: "
                     + ", ".join(p.immutable_drifted_files),
            remediation=remediation, owner=owner,
        )
    return ProbeResult(
        probe, ProbeOutcome.CONTAINED.value, sev, title,
        evidence="all protected core files match the signed baseline",
        remediation=remediation, owner=owner,
    )


def probe_governance_failclosed(p: DefensivePosture) -> ProbeResult:
    """Attack: submit a blatantly manipulative action. Does the EFH floor veto it?"""
    probe, sev, title = (
        "governance_failclosed", 1,
        "Submit a manipulative/deceptive action past the ethical gate (EFH)",
    )
    owner = "governance"
    remediation = (
        "The EFH deterministic heuristic floor did not flag an obviously "
        "manipulative payload -- audit backend/governance/efh_evaluator.py "
        "patterns and the inviolable axiom catalogue."
    )
    if p.governance_floor_installed is None or p.governance_synthetic_attack_breaches is None:
        return ProbeResult(probe, ProbeOutcome.UNKNOWN.value, sev, title,
                           evidence="EFH posture unavailable",
                           remediation=remediation, owner=owner)
    if p.governance_floor_installed is False:
        return ProbeResult(
            probe, ProbeOutcome.BREACHED.value, sev, title,
            evidence="EFH ethical gate failed to load its inviolable axioms -- "
                     "the fail-closed floor is not installed",
            remediation="Restore the EFH evaluator + axioms so the gate is armed.",
            owner=owner,
        )
    if not p.governance_synthetic_attack_breaches:
        return ProbeResult(
            probe, ProbeOutcome.BREACHED.value, sev, title,
            evidence="EFH cleared a deliberately manipulative synthetic payload "
                     "(expected >= 1 axiom breach)",
            remediation=remediation, owner=owner,
        )
    return ProbeResult(
        probe, ProbeOutcome.CONTAINED.value, sev, title,
        evidence="EFH vetoed the synthetic attack on axioms: "
                 + ", ".join(p.governance_synthetic_attack_breaches),
        remediation=remediation, owner=owner,
    )


def probe_operator_absence_continuity(p: DefensivePosture) -> ProbeResult:
    """Attack: the operator disappears for 30 days. Does the business keep running?"""
    probe, sev, title = (
        "operator_absence_continuity", 2,
        "Operator disappears -- can Samus sustain itself autonomously?",
    )
    owner = "operator"
    remediation = (
        "Autonomous business action is not armed. To harden operator-absence "
        "continuity, enable the master cognition loop (cognitive_loop_enabled) "
        "so the cadence acts, not just observes."
    )
    flags = (
        p.continuity_master_loop_enabled,
        p.continuity_cadence_enabled,
        p.continuity_nightly_consolidation_enabled,
    )
    if any(f is None for f in flags):
        return ProbeResult(probe, ProbeOutcome.UNKNOWN.value, sev, title,
                           evidence="continuity flags unavailable",
                           remediation=remediation, owner=owner)
    master, cadence, nightly = flags
    if master and cadence:
        return ProbeResult(
            probe, ProbeOutcome.CONTAINED.value, sev, title,
            evidence="master cognition loop + cadence are armed -- Samus acts "
                     "autonomously without the operator present",
            remediation=remediation, owner=owner,
        )
    if cadence or nightly:
        return ProbeResult(
            probe, ProbeOutcome.DEGRADED.value, sev, title,
            evidence=f"partial continuity only (master_loop={master}, "
                     f"cadence={cadence}, nightly_consolidation={nightly}) -- "
                     "memory/observation continue but autonomous action is off",
            remediation=remediation, owner=owner,
        )
    return ProbeResult(
        probe, ProbeOutcome.BREACHED.value, sev, title,
        evidence="all autonomous loops disabled -- the business halts the moment "
                 "the operator stops arming it (bus factor = 1)",
        remediation=remediation, owner=owner,
    )


# Live registry -- only probes backed by a real sensor. Extend both this list
# and backend/redteam/sensors.py::sense_posture when adding a probe.
LIVE_PROBES: List[Callable[[DefensivePosture], ProbeResult]] = [
    probe_immutable_integrity,
    probe_governance_failclosed,
    probe_operator_absence_continuity,
]


def run_probes(
    posture: DefensivePosture,
    probes: Optional[List[Callable[[DefensivePosture], ProbeResult]]] = None,
) -> List[ProbeResult]:
    """Run each probe over ``posture``. A probe that raises degrades to UNKNOWN."""
    selected = probes if probes is not None else LIVE_PROBES
    out: List[ProbeResult] = []
    for fn in selected:
        try:
            out.append(fn(posture))
        except Exception as exc:  # noqa: BLE001 -- a broken probe never sinks the pass
            out.append(ProbeResult(
                probe=getattr(fn, "__name__", "unknown_probe"),
                outcome=ProbeOutcome.UNKNOWN.value, severity=3,
                title="probe raised", evidence=f"probe error: {exc}",
            ))
    return out


__all__ = [
    "DefensivePosture",
    "probe_immutable_integrity",
    "probe_governance_failclosed",
    "probe_operator_absence_continuity",
    "LIVE_PROBES",
    "run_probes",
]
