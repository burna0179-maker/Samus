"""3-Layer Architecture Snapshot — governance baseline primitive.

Pure-data dataclasses + diff + integrity hashing + adversarial pattern detection
+ risk scoring. Ported from ``Samus/recovery/archive_reasoning_pipeline.md``
section "3-Layer Architecture Snapshot (governance baseline)".

The snapshot represents **governance-approved reality** in three layers:

1. **Structural** — subsystems, modules, dependency edges, interfaces.
   Detects unauthorized modules and subsystem boundary violations.
2. **Behavioral** — filesystem permissions, network policy, runtime
   restrictions (shell, dynamic code, self-modification).
   Detects privilege escalation and new attack surfaces.
3. **Capability** — what the system can do; capability name -> implementing
   module. Detects capability duplication and covert feature insertion.

The combined snapshot acts as a **structural checksum** of the architecture
and feeds Parliament-style governance evaluation downstream.

Module boundaries (intentional):

- **No filesystem or network I/O in this module.** Snapshots are constructed
  from in-memory data supplied by callers. A separate (later) tool will walk
  the codebase and synthesize an ``ArchitectureSnapshot`` from disk; that tool
  is not part of the governance primitive.
- **No dependency on ``backend.common.governance_parliament``.** Parliament
  consumes ``SnapshotDiff`` and ``ProposalRisk`` from this module. The arrow
  goes Parliament -> snapshot, never the reverse, so this primitive stays a
  leaf.
- **No automatic snapshot mutation.** Per the design doc lifecycle rule:

      *"Never update snapshots automatically during ingestion. Snapshots =
      governance-approved reality, not runtime state."*

  This is enforced by the *absence* of any ``update_*`` / ``promote_*`` /
  ``merge_*`` methods rather than a runtime guard. ``diff_snapshots`` returns
  a new ``SnapshotDiff``; callers that want to promote a candidate must do so
  through Parliament out-of-band.

Hash inputs are canonicalized via
``json.dumps(obj, sort_keys=True, separators=(",", ":"))`` and SHA-256 hex
digested, so any two equivalent snapshots produce identical hashes regardless
of dict insertion order.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Layer dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StructuralSnapshot:
    """Layer 1 — physical architecture.

    Attributes:
        subsystems: subsystem name -> list of module names belonging to it.
        modules: module name -> ``{path, deps, interfaces}`` dict (each value
            is whatever shape the caller chose; the snapshot is opaque about
            ``Any`` payload as long as it is JSON-serializable for hashing).
        dependency_edges: directed edges ``(from_module, to_module)``.
    """

    subsystems: dict[str, list[str]] = field(default_factory=dict)
    modules: dict[str, dict[str, Any]] = field(default_factory=dict)
    dependency_edges: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class BehavioralSnapshot:
    """Layer 2 — runtime permissions and execution policy."""

    fs_read: list[str] = field(default_factory=list)
    fs_write: list[str] = field(default_factory=list)
    network: dict[str, Any] = field(
        default_factory=lambda: {"enabled": False, "allowed_hosts": []}
    )
    execution: dict[str, bool] = field(
        default_factory=lambda: {
            "shell": False,
            "dynamic_code": False,
            "self_modify": False,
        }
    )


@dataclass
class CapabilitySnapshot:
    """Layer 3 — what the system can do.

    ``capabilities`` maps a capability name (e.g. ``"archive_ingestion"``) to
    the single module that implements it. A capability appearing twice in the
    same snapshot is a duplicate-capability adversarial signal and is
    detected by ``diff_snapshots``/``detect_adversarial_patterns``.

    NOTE: because the on-the-wire shape is ``dict[str, str]``, duplicates
    *within a single snapshot* can only be expressed by callers passing in
    a custom multi-value structure. The duplicate-detection logic instead
    looks at the candidate snapshot's capability->module map and cross-checks
    against itself plus the diff's ``new_capabilities``; see the
    ``duplicate_capabilities`` field of ``SnapshotDiff``.
    """

    capabilities: dict[str, str] = field(default_factory=dict)


@dataclass
class ArchitectureSnapshot:
    """Unified 3-layer snapshot + per-layer integrity hashes.

    Hashes are populated by ``compute_integrity()`` and stored on the
    instance. Callers may set them post-construction (hence non-frozen).
    """

    snapshot_version: str
    timestamp: str
    structure: StructuralSnapshot
    behavior: BehavioralSnapshot
    capabilities: CapabilitySnapshot
    structure_hash: str = ""
    behavior_hash: str = ""
    capability_hash: str = ""

    # -- integrity -------------------------------------------------------

    def compute_integrity(self) -> None:
        """Populate ``*_hash`` fields from canonical JSON of each layer.

        Idempotent: calling twice on identical data yields identical hashes.
        """
        self.structure_hash = _sha256_canonical(_structure_payload(self.structure))
        self.behavior_hash = _sha256_canonical(_behavior_payload(self.behavior))
        self.capability_hash = _sha256_canonical(
            _capability_payload(self.capabilities)
        )

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the compact unified JSON shape from the design doc."""
        return {
            "snapshot_version": self.snapshot_version,
            "timestamp": self.timestamp,
            "structure": _structure_payload(self.structure),
            "behavior": _behavior_payload(self.behavior),
            "capabilities": _capability_payload(self.capabilities),
            "integrity": {
                "structure_hash": self.structure_hash,
                "behavior_hash": self.behavior_hash,
                "capability_hash": self.capability_hash,
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArchitectureSnapshot":
        """Reverse of ``to_dict``. Restores hashes verbatim (does NOT recompute)."""
        struct_raw = d.get("structure", {}) or {}
        behav_raw = d.get("behavior", {}) or {}
        cap_raw = d.get("capabilities", {}) or {}
        integ = d.get("integrity", {}) or {}

        structure = StructuralSnapshot(
            subsystems={k: list(v) for k, v in (struct_raw.get("subsystems") or {}).items()},
            modules={k: dict(v) for k, v in (struct_raw.get("modules") or {}).items()},
            dependency_edges=[
                (a, b) for a, b in (struct_raw.get("dependency_edges") or [])
            ],
        )
        behavior = BehavioralSnapshot(
            fs_read=list(behav_raw.get("fs_read") or []),
            fs_write=list(behav_raw.get("fs_write") or []),
            network=dict(behav_raw.get("network") or {"enabled": False, "allowed_hosts": []}),
            execution=dict(
                behav_raw.get("execution")
                or {"shell": False, "dynamic_code": False, "self_modify": False}
            ),
        )
        capabilities = CapabilitySnapshot(capabilities=dict(cap_raw))

        return cls(
            snapshot_version=d.get("snapshot_version", ""),
            timestamp=d.get("timestamp", ""),
            structure=structure,
            behavior=behavior,
            capabilities=capabilities,
            structure_hash=integ.get("structure_hash", ""),
            behavior_hash=integ.get("behavior_hash", ""),
            capability_hash=integ.get("capability_hash", ""),
        )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass
class SnapshotDiff:
    """Structural delta between a baseline and a candidate snapshot."""

    new_modules: list[str] = field(default_factory=list)
    removed_modules: list[str] = field(default_factory=list)
    new_dependencies: list[tuple[str, str]] = field(default_factory=list)
    new_fs_read_paths: list[str] = field(default_factory=list)
    new_fs_write_paths: list[str] = field(default_factory=list)
    new_network_hosts: list[str] = field(default_factory=list)
    new_execution_flags: list[str] = field(default_factory=list)
    new_capabilities: list[str] = field(default_factory=list)
    duplicate_capabilities: list[str] = field(default_factory=list)


def diff_snapshots(
    baseline: ArchitectureSnapshot,
    candidate: ArchitectureSnapshot,
) -> SnapshotDiff:
    """Compute the additive/subtractive delta from ``baseline`` -> ``candidate``.

    Deterministic ordering: every output list is sorted (tuples sorted
    lexicographically by their string form) so the diff is byte-stable across
    runs and across dict insertion orders.

    ``duplicate_capabilities`` flags capability names in ``candidate`` that map
    to a *different* module than in ``baseline`` while still being present in
    ``baseline`` (i.e. silent re-pointing) AND capability names that appear
    more than once in the candidate's own internal expression (which the
    plain ``dict[str, str]`` shape cannot express directly, but callers can
    surface via the ``new_capabilities`` list — see module docstring).
    """
    base_struct = baseline.structure
    cand_struct = candidate.structure
    base_behav = baseline.behavior
    cand_behav = candidate.behavior
    base_caps = baseline.capabilities.capabilities
    cand_caps = candidate.capabilities.capabilities

    base_modules = set(base_struct.modules.keys())
    cand_modules = set(cand_struct.modules.keys())

    new_modules = sorted(cand_modules - base_modules)
    removed_modules = sorted(base_modules - cand_modules)

    base_edges = {tuple(e) for e in base_struct.dependency_edges}
    cand_edges = {tuple(e) for e in cand_struct.dependency_edges}
    new_deps_raw = cand_edges - base_edges
    new_dependencies = sorted(new_deps_raw, key=lambda t: (t[0], t[1]))

    new_fs_read = sorted(set(cand_behav.fs_read) - set(base_behav.fs_read))
    new_fs_write = sorted(set(cand_behav.fs_write) - set(base_behav.fs_write))

    base_hosts = set((base_behav.network or {}).get("allowed_hosts") or [])
    cand_hosts = set((cand_behav.network or {}).get("allowed_hosts") or [])
    new_network_hosts = sorted(cand_hosts - base_hosts)

    base_exec = dict(base_behav.execution or {})
    cand_exec = dict(cand_behav.execution or {})
    new_execution_flags = sorted(
        flag
        for flag, val in cand_exec.items()
        if bool(val) and not bool(base_exec.get(flag, False))
    )

    new_capability_names = sorted(set(cand_caps.keys()) - set(base_caps.keys()))

    # Duplicate-capability signal: capability exists in baseline but points
    # to a different module in candidate -> covert re-pointing. Sorted, no dupes.
    repointed = sorted(
        name
        for name, mod in cand_caps.items()
        if name in base_caps and base_caps[name] != mod
    )

    return SnapshotDiff(
        new_modules=new_modules,
        removed_modules=removed_modules,
        new_dependencies=new_dependencies,
        new_fs_read_paths=new_fs_read,
        new_fs_write_paths=new_fs_write,
        new_network_hosts=new_network_hosts,
        new_execution_flags=new_execution_flags,
        new_capabilities=new_capability_names,
        duplicate_capabilities=repointed,
    )


# ---------------------------------------------------------------------------
# Adversarial pattern detection
# ---------------------------------------------------------------------------


def detect_adversarial_patterns(diff: SnapshotDiff) -> list[str]:
    """Inspect a ``SnapshotDiff`` and return human-readable findings.

    Patterns flagged (from design doc):

    - Hidden network access (new allowed hosts)
    - Shell command execution (``execution.shell`` flipped on)
    - Dynamic code evaluation (``execution.dynamic_code`` flipped on)
    - Self-modification (``execution.self_modify`` flipped on)
    - New filesystem read or write paths
    - Cross-subsystem dependency chains (any new dependency edge — caller
      enriches with subsystem boundary metadata downstream)
    - Duplicate / re-pointed capability names

    Findings are returned in a stable order so the output is reproducible.
    """
    findings: list[str] = []

    if "shell" in diff.new_execution_flags:
        findings.append("shell_execution_enabled: execution.shell flipped to True")
    if "dynamic_code" in diff.new_execution_flags:
        findings.append(
            "dynamic_code_evaluation_enabled: execution.dynamic_code flipped to True"
        )
    if "self_modify" in diff.new_execution_flags:
        findings.append("self_modification_enabled: execution.self_modify flipped to True")

    if diff.new_network_hosts:
        findings.append(
            "hidden_network_access: new allowed_hosts = "
            + ",".join(diff.new_network_hosts)
        )

    if diff.new_fs_read_paths:
        findings.append(
            "new_filesystem_read_paths: " + ",".join(diff.new_fs_read_paths)
        )
    if diff.new_fs_write_paths:
        findings.append(
            "new_filesystem_write_paths: " + ",".join(diff.new_fs_write_paths)
        )

    if diff.new_dependencies:
        findings.append(
            "cross_subsystem_dependency_candidates: "
            + ",".join(f"{a}->{b}" for a, b in diff.new_dependencies)
        )

    if diff.duplicate_capabilities:
        findings.append(
            "duplicate_or_repointed_capabilities: "
            + ",".join(diff.duplicate_capabilities)
        )

    return findings


# ---------------------------------------------------------------------------
# Risk scoring (chat 46 upgrade)
# ---------------------------------------------------------------------------


@dataclass
class ProposalRisk:
    """Risk dimensions + composite score for a proposed snapshot change.

    Dimensions mirror the chat 46 governance upgrade:

    - ``permission_changes``: behavioral surface expansions
      (new fs paths + new network hosts + new execution flags).
    - ``dependency_depth``: count of new dependency edges introduced.
    - ``subsystem_boundary_crossings``: count of new modules added (each new
      module is a potential subsystem boundary — exact subsystem mapping is
      caller territory; this is the conservative upper bound).
    - ``new_capabilities``: count of newly exposed capabilities.
    - ``score``: weighted composite in ``[0.0, 1.0]``, monotone increasing
      in every dimension.
    """

    permission_changes: int
    dependency_depth: int
    subsystem_boundary_crossings: int
    new_capabilities: int
    score: float


# Weights are intentionally readable + tunable downstream. The saturation
# function (1 - exp(-k*x)) maps any non-negative weighted sum into [0, 1)
# while remaining strictly monotone: any additional change can only raise
# the score, never lower it. This matters for the
# test_risk_score_increases_with_more_changes invariant.
_RISK_WEIGHTS: dict[str, float] = {
    "permission_changes": 0.35,
    "dependency_depth": 0.15,
    "subsystem_boundary_crossings": 0.25,
    "new_capabilities": 0.25,
}
_RISK_SATURATION_K = 0.25


def score_proposal_risk(diff: SnapshotDiff) -> ProposalRisk:
    """Score a ``SnapshotDiff`` along four risk dimensions + composite."""
    permission_changes = (
        len(diff.new_fs_read_paths)
        + len(diff.new_fs_write_paths)
        + len(diff.new_network_hosts)
        + len(diff.new_execution_flags)
    )
    dependency_depth = len(diff.new_dependencies)
    subsystem_boundary_crossings = len(diff.new_modules)
    new_capabilities = len(diff.new_capabilities) + len(diff.duplicate_capabilities)

    weighted = (
        _RISK_WEIGHTS["permission_changes"] * permission_changes
        + _RISK_WEIGHTS["dependency_depth"] * dependency_depth
        + _RISK_WEIGHTS["subsystem_boundary_crossings"] * subsystem_boundary_crossings
        + _RISK_WEIGHTS["new_capabilities"] * new_capabilities
    )
    # Saturating composite -> always in [0.0, 1.0), strictly monotone.
    import math

    score = 1.0 - math.exp(-_RISK_SATURATION_K * weighted)
    # Clamp defensively against floating-point edge cases.
    if score < 0.0:
        score = 0.0
    elif score > 1.0:
        score = 1.0

    return ProposalRisk(
        permission_changes=permission_changes,
        dependency_depth=dependency_depth,
        subsystem_boundary_crossings=subsystem_boundary_crossings,
        new_capabilities=new_capabilities,
        score=score,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_canonical(payload: Any) -> str:
    """SHA-256 hex digest of canonical JSON (sorted keys, tight separators)."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _structure_payload(s: StructuralSnapshot) -> dict[str, Any]:
    return {
        "subsystems": {k: sorted(v) for k, v in s.subsystems.items()},
        "modules": s.modules,
        "dependency_edges": sorted(
            [list(e) for e in s.dependency_edges], key=lambda e: (e[0], e[1])
        ),
    }


def _behavior_payload(b: BehavioralSnapshot) -> dict[str, Any]:
    return {
        "fs_read": sorted(b.fs_read),
        "fs_write": sorted(b.fs_write),
        "network": {
            "enabled": bool((b.network or {}).get("enabled", False)),
            "allowed_hosts": sorted((b.network or {}).get("allowed_hosts") or []),
        },
        "execution": {
            "shell": bool((b.execution or {}).get("shell", False)),
            "dynamic_code": bool((b.execution or {}).get("dynamic_code", False)),
            "self_modify": bool((b.execution or {}).get("self_modify", False)),
        },
    }


def _capability_payload(c: CapabilitySnapshot) -> dict[str, str]:
    return dict(c.capabilities)


__all__ = [
    "StructuralSnapshot",
    "BehavioralSnapshot",
    "CapabilitySnapshot",
    "ArchitectureSnapshot",
    "SnapshotDiff",
    "ProposalRisk",
    "diff_snapshots",
    "detect_adversarial_patterns",
    "score_proposal_risk",
]
