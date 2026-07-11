"""Concept 7 — propose_next_experiment (hypothesize step)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Isolation — self-contained state root; no live belief/guidance/approval I/O
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    from backend.attribution import store as attr_store
    attr_store.reset_store()
    yield
    attr_store.reset_store()


# ---------------------------------------------------------------------------
# Test doubles — read-only stand-ins for belief_ledger + guidance + approvals
# ---------------------------------------------------------------------------
@dataclass
class _FakeBelief:
    belief_id: str
    claim: str
    confidence: float = 0.5
    economic_impact: float = 0.0
    tier: int = 2
    last_verified: str = ""       # empty -> stale


@dataclass
class _FakeGuidance:
    recommendation_id: str
    action: str = ""
    rationale: str = ""
    tier: int = 2


def _patch_belief_reads(
    monkeypatch,
    *,
    stale: list[_FakeBelief] | None = None,
    settled_claims: list[str] | None = None,
) -> None:
    """Swap the two belief_ledger reads used by propose_next_experiment."""
    from backend.experiments import registry

    def _stale():
        return list(stale or [])

    def _list_all(*, status: str | None = None):
        return [
            _FakeBelief(
                belief_id=f"settled-{i}", claim=c,
                confidence=0.95, economic_impact=100.0, tier=2,
                last_verified="2999-01-01T00:00:00+00:00",
            )
            for i, c in enumerate(settled_claims or [])
        ]

    # belief_ledger module is imported lazily inside registry; patch the
    # module symbols so both reads see the fakes.
    from backend.cognitive import belief_ledger as bl_mod
    monkeypatch.setattr(bl_mod, "stale_beliefs", _stale)
    monkeypatch.setattr(bl_mod, "list_beliefs", _list_all)


def _patch_guidance(monkeypatch, guidances: list[_FakeGuidance]) -> None:
    from backend.cognitive import guidance as g_mod

    class _FakeLedger:
        def __init__(self, *a, **kw): pass
        def all_latest(self):
            return list(guidances)

    monkeypatch.setattr(g_mod, "GuidanceLedger", _FakeLedger)


def _patch_approvals(monkeypatch) -> list[dict[str, Any]]:
    from backend.common import approvals as ap_mod

    seen: list[dict[str, Any]] = []

    def _fake_create(kind, payload=None, *, risk_level="normal",
                     ev_usd=None, confidence=None, ttl_seconds=None):
        row = {
            "id": f"appr-{len(seen)+1:03d}",
            "kind": kind,
            "payload": dict(payload or {}),
            "risk_level": risk_level,
            "ev_usd": ev_usd,
        }
        seen.append(row)
        return row

    monkeypatch.setattr(ap_mod, "create_approval", _fake_create)
    return seen


# ---------------------------------------------------------------------------
# Happy path — one stale belief becomes a low-risk proposal, auto-promoted
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_low_risk_proposal_is_promoted(self, monkeypatch):
        from backend.experiments import registry

        _patch_belief_reads(monkeypatch, stale=[_FakeBelief(
            belief_id="b-opener-1",
            claim="Curiosity-first openers beat direct openers on cold email",
            confidence=0.5,
            economic_impact=200.0,   # small -> low risk
            tier=3,                   # minor -> low risk
        )])
        _patch_guidance(monkeypatch, [_FakeGuidance("rec-1", "test opener")])
        seen_approvals = _patch_approvals(monkeypatch)

        proposal = registry.propose_next_experiment()

        assert proposal is not None
        assert proposal.dimension == "opener"
        assert proposal.candidate_arm == "curiosity-first"
        assert proposal.status == registry.PROPOSAL_STATUS_PROMOTED
        assert proposal.risk_score <= registry.PROPOSAL_RISK_GATE
        assert proposal.source_belief_ids == ["b-opener-1"]
        assert proposal.min_sample_size == registry.DEFAULT_PROPOSAL_SAMPLE_SIZE
        assert proposal.success_metric == "reply_rate"
        # promoted -> a live experiment exists on the right dimension
        active = [e for e in registry.list_experiments(status="active")
                  if e.dimension == "opener"]
        assert active, "expected the proposal to register an active experiment"
        assert proposal.candidate_arm in active[0].arms
        # no HOTL approval used on the low-risk path
        assert seen_approvals == []

    def test_proposal_appears_in_ledger(self, monkeypatch):
        from backend.experiments import registry

        _patch_belief_reads(monkeypatch, stale=[_FakeBelief(
            belief_id="b-1",
            claim="Curiosity openers work",
            economic_impact=100.0, tier=3,
        )])
        _patch_guidance(monkeypatch, [])
        _patch_approvals(monkeypatch)

        registry.propose_next_experiment()
        rows = registry.list_proposals()
        assert len(rows) == 1
        assert rows[0]["status"] == registry.PROPOSAL_STATUS_PROMOTED
        assert rows[0]["dimension"] == "opener"


# ---------------------------------------------------------------------------
# Safety gate — high-risk proposal blocks on HOTL approval
# ---------------------------------------------------------------------------
class TestSafetyGate:
    def test_high_risk_proposal_blocks_on_hotl(self, monkeypatch):
        from backend.experiments import registry

        _patch_belief_reads(monkeypatch, stale=[_FakeBelief(
            belief_id="b-pricing",
            # dimension detection keys on "pricing" -> pricing_tier (0.70 base)
            claim="Annual pricing tier converts higher than monthly",
            confidence=0.4,
            economic_impact=5_000.0,   # large -> impact component pushes up
            tier=1,                     # critical tier -> risk component
        )])
        _patch_guidance(monkeypatch, [])
        seen_approvals = _patch_approvals(monkeypatch)

        proposal = registry.propose_next_experiment()

        assert proposal is not None
        assert proposal.dimension == "pricing_tier"
        assert proposal.risk_score > registry.PROPOSAL_RISK_GATE
        assert proposal.status == registry.PROPOSAL_STATUS_BLOCKED
        assert proposal.approval_id.startswith("appr-")
        # HOTL wire-through
        assert len(seen_approvals) == 1
        row = seen_approvals[0]
        assert row["kind"] == "experiment_proposal"
        # ADR-0019: high/critical -> emergency severity in approvals.severity_for
        assert row["risk_level"] in ("high", "critical")
        assert row["payload"]["proposal_id"] == proposal.proposal_id
        assert row["payload"]["dimension"] == "pricing_tier"
        # High-risk path must NOT auto-register the experiment
        active_pricing = [e for e in registry.list_experiments(status="active")
                         if e.dimension == "pricing_tier"]
        assert active_pricing == []


# ---------------------------------------------------------------------------
# Belief-avoidance — do NOT re-test a settled belief
# ---------------------------------------------------------------------------
class TestBeliefAvoidance:
    def test_settled_belief_is_skipped(self, monkeypatch):
        from backend.experiments import registry

        settled_claim = "Curiosity-first openers beat direct openers on cold email"
        _patch_belief_reads(
            monkeypatch,
            stale=[_FakeBelief(
                belief_id="b-1",
                claim=settled_claim,
                economic_impact=100.0, tier=3,
            )],
            settled_claims=[settled_claim],
        )
        _patch_guidance(monkeypatch, [])
        _patch_approvals(monkeypatch)

        result = registry.propose_next_experiment()

        # No actionable observation -> None + a SKIPPED row for auditability
        assert result is None
        rows = registry.list_proposals(status=registry.PROPOSAL_STATUS_SKIPPED)
        assert len(rows) == 1
        assert rows[0]["metadata"]["reason"] == "no_actionable_observation"
        # No experiment registered
        assert registry.list_experiments(status="active") == []

    def test_covered_arm_forces_next_variant(self, monkeypatch):
        from backend.experiments import registry

        # Pre-cover the deterministic default candidate so proposer must
        # generate the next slug in the sequence.
        registry.register_experiment(
            dimension="opener", arms=["direct", "curiosity-first"],
            experiment_id="pre-existing",
        )
        _patch_belief_reads(monkeypatch, stale=[_FakeBelief(
            belief_id="b-1",
            claim="A new opener style should beat the incumbent",
            economic_impact=50.0, tier=3,
        )])
        _patch_guidance(monkeypatch, [])
        _patch_approvals(monkeypatch)

        proposal = registry.propose_next_experiment()
        assert proposal is not None
        assert proposal.dimension == "opener"
        assert proposal.candidate_arm != "curiosity-first"
        assert proposal.candidate_arm.startswith("curiosity-first-")


# ---------------------------------------------------------------------------
# Empty observations edge case
# ---------------------------------------------------------------------------
class TestEmptyObservations:
    def test_no_stale_beliefs_returns_none(self, monkeypatch):
        from backend.experiments import registry

        _patch_belief_reads(monkeypatch, stale=[])
        _patch_guidance(monkeypatch, [])
        seen_approvals = _patch_approvals(monkeypatch)

        result = registry.propose_next_experiment()

        assert result is None
        # Skipped row still written for observability
        rows = registry.list_proposals()
        assert len(rows) == 1
        assert rows[0]["status"] == registry.PROPOSAL_STATUS_SKIPPED
        # No HOTL noise
        assert seen_approvals == []
        # No experiment created
        assert registry.list_experiments(status="active") == []
