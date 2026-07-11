"""Tests for the 3-layer architecture snapshot governance primitive.

Pure-data: every fixture is built inline, no filesystem or network I/O.
"""

from __future__ import annotations


from backend.common.architecture_snapshot import (
    ArchitectureSnapshot,
    BehavioralSnapshot,
    CapabilitySnapshot,
    SnapshotDiff,
    StructuralSnapshot,
    detect_adversarial_patterns,
    diff_snapshots,
    score_proposal_risk,
)


# ---------------------------------------------------------------------------
# Inline fixtures (plain helpers, deliberately not pytest fixtures so each
# test owns its data and there is no shared mutable state).
# ---------------------------------------------------------------------------


def _baseline_snapshot() -> ArchitectureSnapshot:
    structure = StructuralSnapshot(
        subsystems={
            "archive": ["archive_ingest", "archive_router"],
            "knowledge": ["concept_extractor"],
        },
        modules={
            "archive_ingest": {
                "path": "services/archive_ingest.py",
                "deps": ["filesystem", "document_parser"],
                "interfaces": ["scan_incoming", "route_archive"],
            },
            "archive_router": {
                "path": "services/archive_router.py",
                "deps": ["archive_ingest"],
                "interfaces": ["route"],
            },
            "concept_extractor": {
                "path": "knowledge/concept_extractor.py",
                "deps": [],
                "interfaces": ["extract"],
            },
        },
        dependency_edges=[
            ("archive_ingest", "document_parser"),
            ("archive_router", "archive_ingest"),
        ],
    )
    behavior = BehavioralSnapshot(
        fs_read=["archives/incoming", "knowledge/"],
        fs_write=["archives/processed", "proposals/"],
        network={"enabled": False, "allowed_hosts": []},
        execution={"shell": False, "dynamic_code": False, "self_modify": False},
    )
    capabilities = CapabilitySnapshot(
        capabilities={
            "archive_ingestion": "archive_ingest",
            "concept_extraction": "concept_extractor",
        }
    )
    snap = ArchitectureSnapshot(
        snapshot_version="1.0",
        timestamp="2026-05-17T00:00:00Z",
        structure=structure,
        behavior=behavior,
        capabilities=capabilities,
    )
    snap.compute_integrity()
    return snap


# ---------------------------------------------------------------------------
# Integrity hash tests
# ---------------------------------------------------------------------------


def test_integrity_hash_deterministic():
    a = _baseline_snapshot()
    b = _baseline_snapshot()
    assert a.structure_hash == b.structure_hash
    assert a.behavior_hash == b.behavior_hash
    assert a.capability_hash == b.capability_hash
    # And each is a 64-char sha256 hex digest
    assert len(a.structure_hash) == 64
    assert len(a.behavior_hash) == 64
    assert len(a.capability_hash) == 64


def test_integrity_hash_deterministic_under_dict_insertion_order():
    """Different dict insertion order must yield the same hash."""
    snap1 = _baseline_snapshot()

    # Build an equivalent snapshot with different insertion ordering.
    structure = StructuralSnapshot(
        subsystems={
            # Reversed insertion order vs baseline
            "knowledge": ["concept_extractor"],
            "archive": ["archive_router", "archive_ingest"],  # reversed list
        },
        modules={
            # Insert in reversed key order
            "concept_extractor": {
                "interfaces": ["extract"],
                "deps": [],
                "path": "knowledge/concept_extractor.py",
            },
            "archive_router": {
                "interfaces": ["route"],
                "deps": ["archive_ingest"],
                "path": "services/archive_router.py",
            },
            "archive_ingest": {
                "interfaces": ["scan_incoming", "route_archive"],
                "deps": ["filesystem", "document_parser"],
                "path": "services/archive_ingest.py",
            },
        },
        dependency_edges=[
            ("archive_router", "archive_ingest"),
            ("archive_ingest", "document_parser"),
        ],
    )
    snap2 = ArchitectureSnapshot(
        snapshot_version="1.0",
        timestamp="2026-05-17T00:00:00Z",
        structure=structure,
        behavior=snap1.behavior,
        capabilities=snap1.capabilities,
    )
    snap2.compute_integrity()

    assert snap1.structure_hash == snap2.structure_hash


def test_integrity_hash_changes_on_module_added():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.structure.modules["new_module"] = {
        "path": "services/new_module.py",
        "deps": [],
        "interfaces": [],
    }
    candidate.compute_integrity()

    assert candidate.structure_hash != baseline.structure_hash


def test_integrity_hash_isolated_per_layer():
    """Adding a capability must NOT change structure_hash or behavior_hash."""
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.capabilities.capabilities["new_cap"] = "concept_extractor"
    candidate.compute_integrity()

    assert candidate.capability_hash != baseline.capability_hash
    assert candidate.structure_hash == baseline.structure_hash
    assert candidate.behavior_hash == baseline.behavior_hash


def test_integrity_hash_changes_on_behavior_flip():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.execution["shell"] = True
    candidate.compute_integrity()

    assert candidate.behavior_hash != baseline.behavior_hash
    assert candidate.structure_hash == baseline.structure_hash
    assert candidate.capability_hash == baseline.capability_hash


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_dict_roundtrip():
    original = _baseline_snapshot()
    restored = ArchitectureSnapshot.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    # Hashes survive the roundtrip without recomputation
    assert restored.structure_hash == original.structure_hash
    assert restored.behavior_hash == original.behavior_hash
    assert restored.capability_hash == original.capability_hash


def test_to_dict_shape_matches_design_doc():
    snap = _baseline_snapshot()
    d = snap.to_dict()
    # Required top-level keys per the design doc compact unified snapshot
    assert set(d.keys()) == {
        "snapshot_version",
        "timestamp",
        "structure",
        "behavior",
        "capabilities",
        "integrity",
    }
    assert set(d["structure"].keys()) == {"subsystems", "modules", "dependency_edges"}
    assert set(d["behavior"].keys()) == {"fs_read", "fs_write", "network", "execution"}
    assert set(d["integrity"].keys()) == {
        "structure_hash",
        "behavior_hash",
        "capability_hash",
    }


# ---------------------------------------------------------------------------
# Diff tests
# ---------------------------------------------------------------------------


def test_diff_detects_new_module():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.structure.modules["proposal_router"] = {
        "path": "governance/proposal_router.py",
        "deps": [],
        "interfaces": [],
    }
    diff = diff_snapshots(baseline, candidate)
    assert diff.new_modules == ["proposal_router"]
    assert diff.removed_modules == []


def test_diff_detects_removed_module():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    del candidate.structure.modules["archive_router"]
    diff = diff_snapshots(baseline, candidate)
    assert diff.removed_modules == ["archive_router"]
    assert diff.new_modules == []


def test_diff_detects_new_dependency_edge():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.structure.dependency_edges.append(("concept_extractor", "archive_ingest"))
    diff = diff_snapshots(baseline, candidate)
    assert ("concept_extractor", "archive_ingest") in diff.new_dependencies


def test_diff_detects_new_fs_write_path():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.fs_write.append("/etc/secrets")
    diff = diff_snapshots(baseline, candidate)
    assert diff.new_fs_write_paths == ["/etc/secrets"]
    assert diff.new_fs_read_paths == []


def test_diff_detects_new_fs_read_path():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.fs_read.append("/home/user/.ssh")
    diff = diff_snapshots(baseline, candidate)
    assert diff.new_fs_read_paths == ["/home/user/.ssh"]


def test_diff_detects_shell_execution_enabled():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.execution["shell"] = True
    diff = diff_snapshots(baseline, candidate)
    assert "shell" in diff.new_execution_flags
    # Other flags must NOT be flagged because they didn't flip.
    assert "dynamic_code" not in diff.new_execution_flags
    assert "self_modify" not in diff.new_execution_flags


def test_diff_detects_new_network_host():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.network = {
        "enabled": True,
        "allowed_hosts": ["api.evil.example.com"],
    }
    diff = diff_snapshots(baseline, candidate)
    assert diff.new_network_hosts == ["api.evil.example.com"]


def test_diff_detects_new_capability():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.capabilities.capabilities["covert_action"] = "archive_ingest"
    diff = diff_snapshots(baseline, candidate)
    assert diff.new_capabilities == ["covert_action"]


def test_diff_detects_duplicate_capability():
    """A capability name re-pointed to a different module is a covert
    re-pointing -> ``duplicate_capabilities`` per the design doc.
    """
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    # baseline: concept_extraction -> concept_extractor
    # candidate re-points it to a different module silently
    candidate.capabilities.capabilities["concept_extraction"] = "archive_ingest"
    diff = diff_snapshots(baseline, candidate)
    assert diff.duplicate_capabilities == ["concept_extraction"]
    # And it's NOT also flagged as a new capability (it's a re-point)
    assert "concept_extraction" not in diff.new_capabilities


def test_diff_is_empty_for_identical_snapshots():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    diff = diff_snapshots(baseline, candidate)
    assert diff == SnapshotDiff()


def test_diff_output_is_deterministically_sorted():
    """Two diffs of equivalent inputs (in different order) must compare equal."""
    baseline = _baseline_snapshot()

    cand1 = _baseline_snapshot()
    cand1.structure.modules["m_a"] = {"path": "a.py", "deps": [], "interfaces": []}
    cand1.structure.modules["m_b"] = {"path": "b.py", "deps": [], "interfaces": []}
    cand1.behavior.fs_write.extend(["/zzz", "/aaa"])

    cand2 = _baseline_snapshot()
    cand2.structure.modules["m_b"] = {"path": "b.py", "deps": [], "interfaces": []}
    cand2.structure.modules["m_a"] = {"path": "a.py", "deps": [], "interfaces": []}
    cand2.behavior.fs_write.extend(["/aaa", "/zzz"])

    diff1 = diff_snapshots(baseline, cand1)
    diff2 = diff_snapshots(baseline, cand2)
    assert diff1 == diff2
    assert diff1.new_modules == ["m_a", "m_b"]
    assert diff1.new_fs_write_paths == ["/aaa", "/zzz"]


# ---------------------------------------------------------------------------
# Adversarial-pattern tests
# ---------------------------------------------------------------------------


def test_adversarial_pattern_flags_shell():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.execution["shell"] = True
    diff = diff_snapshots(baseline, candidate)
    findings = detect_adversarial_patterns(diff)
    assert any("shell_execution_enabled" in f for f in findings)


def test_adversarial_pattern_flags_dynamic_code():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.execution["dynamic_code"] = True
    diff = diff_snapshots(baseline, candidate)
    findings = detect_adversarial_patterns(diff)
    assert any("dynamic_code_evaluation_enabled" in f for f in findings)


def test_adversarial_pattern_flags_self_modify():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.execution["self_modify"] = True
    diff = diff_snapshots(baseline, candidate)
    findings = detect_adversarial_patterns(diff)
    assert any("self_modification_enabled" in f for f in findings)


def test_adversarial_pattern_flags_new_network_host():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.behavior.network = {
        "enabled": True,
        "allowed_hosts": ["c2.attacker.example"],
    }
    diff = diff_snapshots(baseline, candidate)
    findings = detect_adversarial_patterns(diff)
    assert any("hidden_network_access" in f and "c2.attacker.example" in f for f in findings)


def test_adversarial_pattern_flags_duplicate_capability():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.capabilities.capabilities["concept_extraction"] = "archive_ingest"
    diff = diff_snapshots(baseline, candidate)
    findings = detect_adversarial_patterns(diff)
    assert any(
        "duplicate_or_repointed_capabilities" in f and "concept_extraction" in f for f in findings
    )


def test_adversarial_patterns_empty_for_identical_snapshots():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    diff = diff_snapshots(baseline, candidate)
    assert detect_adversarial_patterns(diff) == []


# ---------------------------------------------------------------------------
# Risk-scoring tests
# ---------------------------------------------------------------------------


def test_risk_score_zero_for_identical_snapshots():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    diff = diff_snapshots(baseline, candidate)
    risk = score_proposal_risk(diff)
    assert risk.permission_changes == 0
    assert risk.dependency_depth == 0
    assert risk.subsystem_boundary_crossings == 0
    assert risk.new_capabilities == 0
    assert risk.score == 0.0


def test_risk_score_increases_with_more_changes():
    baseline = _baseline_snapshot()

    # One small change
    small = _baseline_snapshot()
    small.structure.modules["m1"] = {"path": "m1.py", "deps": [], "interfaces": []}
    small_diff = diff_snapshots(baseline, small)
    small_risk = score_proposal_risk(small_diff)

    # Strictly more changes
    big = _baseline_snapshot()
    big.structure.modules["m1"] = {"path": "m1.py", "deps": [], "interfaces": []}
    big.structure.modules["m2"] = {"path": "m2.py", "deps": [], "interfaces": []}
    big.structure.dependency_edges.append(("m1", "archive_ingest"))
    big.behavior.fs_write.append("/etc/shadow")
    big.behavior.execution["shell"] = True
    big.capabilities.capabilities["new_cap"] = "m1"
    big_diff = diff_snapshots(baseline, big)
    big_risk = score_proposal_risk(big_diff)

    assert big_risk.permission_changes > small_risk.permission_changes
    assert big_risk.dependency_depth > small_risk.dependency_depth
    assert big_risk.subsystem_boundary_crossings > small_risk.subsystem_boundary_crossings
    assert big_risk.new_capabilities > small_risk.new_capabilities
    assert big_risk.score > small_risk.score
    # Composite score is always in [0.0, 1.0]
    assert 0.0 <= small_risk.score <= 1.0
    assert 0.0 <= big_risk.score <= 1.0


def test_risk_score_dimensions_match_diff_counts():
    baseline = _baseline_snapshot()
    candidate = _baseline_snapshot()
    candidate.structure.modules["m_x"] = {"path": "x.py", "deps": [], "interfaces": []}
    candidate.structure.dependency_edges.append(("m_x", "archive_ingest"))
    candidate.behavior.fs_read.append("/secret")
    candidate.behavior.fs_write.append("/out")
    candidate.behavior.network = {"enabled": True, "allowed_hosts": ["x.example"]}
    candidate.behavior.execution["dynamic_code"] = True
    candidate.capabilities.capabilities["cap_x"] = "m_x"

    diff = diff_snapshots(baseline, candidate)
    risk = score_proposal_risk(diff)
    # 1 new fs_read + 1 new fs_write + 1 new host + 1 execution flag = 4
    assert risk.permission_changes == 4
    assert risk.dependency_depth == 1
    assert risk.subsystem_boundary_crossings == 1
    assert risk.new_capabilities == 1


# ---------------------------------------------------------------------------
# Module-boundary / lifecycle invariants
# ---------------------------------------------------------------------------


def test_no_automatic_promotion_method_exists():
    """Per the design doc: 'Never update snapshots automatically during
    ingestion.' The module enforces this by *omitting* any update_/promote_/
    merge_ method. This test fails loudly if someone adds one later.
    """
    snap = _baseline_snapshot()
    forbidden_prefixes = ("update_", "promote_", "merge_", "apply_")
    offenders = [
        name
        for name in dir(snap)
        if name.startswith(forbidden_prefixes) and callable(getattr(snap, name))
    ]
    assert offenders == [], (
        f"Forbidden mutation methods on ArchitectureSnapshot: {offenders}. "
        "Snapshots are governance-approved reality, not runtime state."
    )


def test_module_has_no_governance_parliament_import():
    """Module must not depend on governance_parliament (arrow goes the other
    way). Inspect the file's source rather than trying to import the
    downstream module (which doesn't exist on the base branch).
    """
    import inspect

    from backend.common import architecture_snapshot

    src = inspect.getsource(architecture_snapshot)
    assert "governance_parliament" not in src or "No dependency on" in src, (
        "architecture_snapshot must not import governance_parliament"
    )
    # Stricter: there must be NO import line that pulls it in.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "governance_parliament" not in stripped
