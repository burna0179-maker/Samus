"""Experiment registry — registered experiments allocated by the existing UCB1.

An :class:`Experiment` names a **dimension** (what is being varied), its
candidate **arms**, a ``min_trials`` floor before promotion decisions, and a
lifecycle ``status``. Allocation is DELEGATED to the proven UCB1 engine in
:mod:`backend.attribution.engine` (`select_variant` / `record_outcome`) using
namespaced arm ids ``exp::<experiment_id>::<arm>`` — no second bandit.

Persistence (existing patterns):

  * Registry: one JSON document (atomic tmp-replace write) under the state
    root, env-overridable via ``SAMUS_EXPERIMENTS_REGISTRY_PATH``. The arm
    STATS live in the attribution store (DDB primary + JSON fallback), so the
    registry itself only carries definitions — cheap and conflict-free.
  * Assignments: append-only ledger via ``open_ledger`` (jsonl / Firestore
    backend-portable), so any day's selection is replayable. Every assignment
    is also emitted as an ``experiment.assigned`` unified business event via
    the Tranche-1 shim (no-op pre-merge).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.common.business_events_shim_t3 import emit_business_event
from backend.common.dates import iso_now
from backend.common.persistence import open_ledger
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.experiments.registry")

ARM_NAMESPACE_SEP = "::"
ENV_REGISTRY_PATH = "SAMUS_EXPERIMENTS_REGISTRY_PATH"

_REGISTRY_JSONL_DEFAULT = ("experiments", "registry.json")
_ASSIGNMENTS_JSONL = ("experiments", "assignments.jsonl")
_ASSIGNMENTS_COLLECTION = "experiment_assignments"
_PROPOSALS_JSONL = ("experiments", "proposals.jsonl")
_PROPOSALS_COLLECTION = "experiment_proposals"

DIMENSIONS = frozenset({
    "opener", "offer", "pricing_tier", "cadence",
    "call_script", "subject", "cta", "landing",
})

STATUSES = frozenset({"active", "paused", "concluded"})

DEFAULT_MIN_TRIALS = 30

# Risk score above this threshold requires HOTL approval before the proposal
# can be promoted to an active experiment (ADR-0019 severity + TTL).
PROPOSAL_RISK_GATE = 0.5

# Default sample-size floor for a proposed arm — matches ``DEFAULT_MIN_TRIALS``
# so a proposal handed to ``register_experiment`` never underruns significance.
DEFAULT_PROPOSAL_SAMPLE_SIZE = DEFAULT_MIN_TRIALS

PROPOSAL_STATUS_PENDING = "pending"          # not yet gated / promoted
PROPOSAL_STATUS_BLOCKED = "blocked_on_hotl"  # awaiting operator approval
PROPOSAL_STATUS_PROMOTED = "promoted"        # became an active experiment
PROPOSAL_STATUS_SKIPPED = "skipped"          # nothing to propose from context

_LOCK = threading.Lock()


@dataclass
class Experiment:
    """One registered experiment definition (stats live in the bandit store)."""

    experiment_id: str
    dimension: str
    arms: list[str] = field(default_factory=list)
    min_trials: int = DEFAULT_MIN_TRIALS
    status: str = "active"
    archived_arms: list[str] = field(default_factory=list)
    # arm -> minimum allocation share [0,1]; raised by the promoter for
    # significant winners. Advisory to allocation consumers.
    allocation_floors: dict[str, float] = field(default_factory=dict)
    # The baseline/control arm this experiment measures uplift AGAINST (the
    # incumbent tactic). Empty -> the causal uplift module falls back to the
    # highest-trial arm as the de-facto baseline. This is what turns a purely
    # relative "which arm wins" bandit into a causal "does this arm beat the
    # incumbent" estimate — see backend/experiments/uplift.py.
    control_arm: str = ""
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Experiment":
        return cls(
            experiment_id=str(row.get("experiment_id", "")),
            dimension=str(row.get("dimension", "")),
            arms=[str(a) for a in (row.get("arms") or [])],
            min_trials=int(row.get("min_trials", DEFAULT_MIN_TRIALS) or DEFAULT_MIN_TRIALS),
            status=str(row.get("status", "active") or "active"),
            archived_arms=[str(a) for a in (row.get("archived_arms") or [])],
            allocation_floors={
                str(k): float(v)
                for k, v in (row.get("allocation_floors") or {}).items()
            },
            control_arm=str(row.get("control_arm", "") or ""),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
            metadata=dict(row.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# Registry persistence (JSON document, atomic replace)
# ---------------------------------------------------------------------------
def _registry_path() -> Path:
    override = (os.getenv(ENV_REGISTRY_PATH) or "").strip()
    if override:
        return Path(override)
    return state_path(*_REGISTRY_JSONL_DEFAULT)


def _load_all() -> dict[str, dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        _LOG.warning("experiments registry load failed: %s", exc)
        return {}


def _write_all(data: dict[str, dict[str, Any]]) -> bool:
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _LOG.warning("experiments registry write failed: %s", exc)
        return False


def _assignments_ledger():
    return open_ledger(
        jsonl_path=state_path(*_ASSIGNMENTS_JSONL),
        collection=_ASSIGNMENTS_COLLECTION,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_experiment_arm_id(experiment_id: str, arm: str) -> str:
    """Namespaced bandit arm id: ``exp::<experiment_id>::<arm>``."""
    return ARM_NAMESPACE_SEP.join(
        ["exp", (experiment_id or "default").strip(), (arm or "default").strip()],
    )


def register_experiment(
    *,
    dimension: str,
    arms: list[str],
    experiment_id: str = "",
    min_trials: int = DEFAULT_MIN_TRIALS,
    metadata: dict[str, Any] | None = None,
) -> Experiment:
    """Register (or replace) an experiment definition.

    Raises ``ValueError`` on an unknown dimension or fewer than 2 arms —
    a one-arm "experiment" is a default, not an experiment.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(
            f"unknown dimension {dimension!r} (expected one of {sorted(DIMENSIONS)})",
        )
    clean_arms = [a.strip() for a in (arms or []) if a and a.strip()]
    if len(clean_arms) < 2:
        raise ValueError("an experiment needs at least 2 arms")
    now = iso_now()
    exp = Experiment(
        experiment_id=(experiment_id or "").strip() or f"{dimension}-{uuid.uuid4().hex[:8]}",
        dimension=dimension,
        arms=clean_arms,
        min_trials=max(1, int(min_trials)),
        status="active",
        created_at=now,
        updated_at=now,
        metadata=dict(metadata or {}),
    )
    with _LOCK:
        data = _load_all()
        prior = data.get(exp.experiment_id)
        if prior:
            exp.created_at = str(prior.get("created_at") or now)
        data[exp.experiment_id] = exp.to_dict()
        _write_all(data)
    return exp


def get_experiment(experiment_id: str) -> Experiment | None:
    row = _load_all().get(experiment_id)
    return Experiment.from_dict(row) if row else None


def list_experiments(*, status: str | None = None) -> list[Experiment]:
    out = [Experiment.from_dict(r) for r in _load_all().values()]
    if status:
        out = [e for e in out if e.status == status]
    return sorted(out, key=lambda e: e.experiment_id)


def save_experiment(exp: Experiment) -> bool:
    """Persist an updated experiment record (promoter transitions use this)."""
    exp.updated_at = iso_now()
    with _LOCK:
        data = _load_all()
        data[exp.experiment_id] = exp.to_dict()
        return _write_all(data)


def assign(
    experiment_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose an arm for one trial of ``experiment_id``.

    Delegates allocation to the existing UCB1 (`attribution.engine
    .select_variant`) over namespaced arm ids; appends the assignment to the
    append-only assignments ledger; emits ``experiment.assigned`` via the
    unified-event shim. Never raises: an unknown/inactive experiment returns
    a structured refusal.
    """
    exp = get_experiment(experiment_id)
    if exp is None:
        return {"ok": False, "error": f"unknown experiment {experiment_id!r}"}
    if exp.status != "active":
        return {"ok": False, "error": f"experiment {experiment_id!r} is {exp.status}"}
    live_arms = [a for a in exp.arms if a not in set(exp.archived_arms)]
    if not live_arms:
        return {"ok": False, "error": f"experiment {experiment_id!r} has no live arms"}

    from backend.attribution.engine import select_variant

    candidates = [build_experiment_arm_id(experiment_id, a) for a in live_arms]
    choice = select_variant(candidates)
    arm = choice.arm_id.split(ARM_NAMESPACE_SEP)[-1] if choice.arm_id else live_arms[0]

    ctx = dict(context or {})
    assignment = {
        "ts": iso_now(),
        "assignment_id": uuid.uuid4().hex,
        "experiment_id": experiment_id,
        "dimension": exp.dimension,
        "arm": arm,
        "arm_id": choice.arm_id or build_experiment_arm_id(experiment_id, arm),
        "reason": choice.reason,
        "context": ctx,
    }
    try:
        _assignments_ledger().append(assignment)
    except Exception as exc:  # noqa: BLE001 — replay ledger is best-effort
        _LOG.warning("assignment ledger append failed: %s", exc)
    emit_business_event(
        "experiment.assigned",
        workcell="experiments",
        prospect_id=ctx.get("prospect_id"),
        opportunity_id=ctx.get("opportunity_id"),
        campaign_id=ctx.get("campaign_id"),
        variant_arm_id=assignment["arm_id"],
        metadata={
            "experiment_id": experiment_id,
            "dimension": exp.dimension,
            "arm": arm,
            "reason": choice.reason,
        },
    )
    return {"ok": True, **assignment}


def record_result(
    experiment_id: str, arm: str, reward: float, *, won: bool = False,
) -> None:
    """Credit one trial outcome to the experiment arm's bandit stats."""
    from backend.attribution.engine import record_outcome

    record_outcome(build_experiment_arm_id(experiment_id, arm), reward, won=won)


def arm_stats(experiment_id: str) -> dict[str, dict[str, Any]]:
    """Per-arm ``{trials, wins, mean_reward}`` from the attribution store.

    Includes archived arms (their history stays readable). Fail-open: a
    store error yields zeroed stats for that arm.
    """
    exp = get_experiment(experiment_id)
    if exp is None:
        return {}
    from backend.attribution import store as attr_store
    from backend.attribution.store import VariantStats

    out: dict[str, dict[str, Any]] = {}
    for arm in list(exp.arms) + [a for a in exp.archived_arms if a not in exp.arms]:
        arm_id = build_experiment_arm_id(experiment_id, arm)
        try:
            stats = attr_store.get_store().load(arm_id) or VariantStats(arm_id=arm_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("arm stats load failed %s: %s", arm_id, exc)
            stats = VariantStats(arm_id=arm_id)
        out[arm] = {
            "arm_id": arm_id,
            "trials": stats.trials,
            "wins": stats.wins,
            "mean_reward": round(stats.mean_reward, 4),
            "archived": arm in set(exp.archived_arms),
        }
    return out


def assignments_tail(limit: int = 100) -> list[dict[str, Any]]:
    """Most recent assignment rows (oldest-first). Fail-open to []."""
    try:
        return _assignments_ledger().tail(limit=limit)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("assignments tail failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Concept 7 — hypothesize step: propose_next_experiment
#
# The observe -> hypothesize -> experiment -> measure -> publish ->
# institutionalize loop is already implemented by registry (experiment) +
# UCB1 (measure) + promoter (publish) + consolidator (institutionalize). The
# only weak link is *hypothesize*: turning observations already surfaced by
# the consolidator + belief ledger into the next-experiment proposal.
#
# This is additive: existing modules (belief_ledger, consolidator, approvals)
# are READ ONLY via their public APIs. Proposals go to a dedicated
# ``experiments/proposals.jsonl`` ledger; high-risk proposals are gated behind
# a ``create_approval`` call (ADR-0019 severity + TTL) before promotion.
# ---------------------------------------------------------------------------
@dataclass
class ExperimentProposal:
    """One proposed next-experiment surfaced from current observations.

    Written to the proposals ledger; promoted to the experiments registry via
    :func:`register_experiment` only after any HOTL approval clears.
    """

    proposal_id: str
    dimension: str
    hypothesis: str
    control_arm: str
    candidate_arm: str
    success_metric: str
    min_sample_size: int = DEFAULT_PROPOSAL_SAMPLE_SIZE
    risk_score: float = 0.0
    expected_economic_impact: float = 0.0
    status: str = PROPOSAL_STATUS_PENDING
    approval_id: str = ""
    source_belief_ids: list[str] = field(default_factory=list)
    source_guidance_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _proposals_ledger():
    return open_ledger(
        jsonl_path=state_path(*_PROPOSALS_JSONL),
        collection=_PROPOSALS_COLLECTION,
    )


def _read_stale_beliefs(min_impact: float) -> list[Any]:
    """Belief-ledger read (public API). Fail-open to []."""
    try:
        from backend.cognitive import belief_ledger

        return [b for b in belief_ledger.stale_beliefs()
                if float(getattr(b, "economic_impact", 0.0) or 0.0) >= min_impact]
    except Exception as exc:  # noqa: BLE001 — cognitive optional at import time
        _LOG.info("belief ledger read failed (%s); proposing without beliefs", exc)
        return []


def _read_settled_beliefs() -> set[str]:
    """Claims that are high-confidence + fresh -> do not re-test them."""
    try:
        from backend.cognitive import belief_ledger

        settled: set[str] = set()
        for b in belief_ledger.list_beliefs(status=belief_ledger.STATUS_ACTIVE):
            conf = float(getattr(b, "confidence", 0.0) or 0.0)
            if conf >= 0.85:
                claim = (getattr(b, "claim", "") or "").strip().lower()
                if claim:
                    settled.add(claim)
        return settled
    except Exception as exc:  # noqa: BLE001
        _LOG.info("settled-belief scan failed (%s); no belief-avoidance", exc)
        return set()


def _read_promoted_patterns() -> list[dict[str, Any]]:
    """Consolidator's promoted lessons (guidance ledger). Fail-open to [].

    These are the DISTILL/PROMOTE outputs the consolidator already emits —
    the exact 'patterns/heuristics' surface the plan says to consume.
    """
    try:
        from backend.cognitive.guidance import GuidanceLedger

        records = GuidanceLedger().all_latest()
        out: list[dict[str, Any]] = []
        for rec in records:
            out.append({
                "recommendation_id": getattr(rec, "recommendation_id", ""),
                "action": (getattr(rec, "action", "") or "").strip(),
                "rationale": (getattr(rec, "rationale", "") or "").strip(),
                "tier": int(getattr(rec, "tier", 2) or 2),
            })
        return out
    except Exception as exc:  # noqa: BLE001 — guidance optional
        _LOG.info("guidance ledger read failed (%s); no promoted patterns", exc)
        return []


def _current_experiment_arms() -> dict[str, set[str]]:
    """Dimension -> set of arms already covered by an active experiment."""
    out: dict[str, set[str]] = {}
    for exp in list_experiments(status="active"):
        out.setdefault(exp.dimension, set()).update(exp.arms)
    return out


def _risk_score(*, dimension: str, expected_impact: float,
                belief_tier: int, stale_days: float) -> float:
    """Bounded [0,1] risk score.

    Higher when: the dimension is side-effect heavy (call_script, cadence,
    pricing_tier — real customer contact / commercial commitment), the
    expected economic impact is large, the load-bearing belief is tier-1,
    or the belief has been unverified long enough that acting on it could
    burn multiple prospects before we detect a regression.
    """
    dimension_risk = {
        "opener": 0.20, "subject": 0.20, "cta": 0.25, "landing": 0.25,
        "offer": 0.35, "call_script": 0.55, "cadence": 0.55,
        "pricing_tier": 0.70,
    }.get(dimension, 0.30)
    # tier-1 (critical) beliefs get the full weight; tier-3 (minor) barely nudges
    tier_component = max(0.0, min(0.30, (4 - max(1, min(3, belief_tier))) * 0.10))
    # $0..$5k linear ramp, capped at $10k
    impact_component = max(0.0, min(0.30, float(expected_impact) / 10_000.0 * 0.30))
    stale_component = 0.0 if stale_days <= 0 else min(0.15, stale_days / 60.0 * 0.15)
    score = dimension_risk + tier_component + impact_component + stale_component
    return round(max(0.0, min(1.0, score)), 3)


def _hypothesis_from_belief(belief: Any, dimension: str, arm: str) -> str:
    claim = (getattr(belief, "claim", "") or "").strip()
    if claim:
        return (f"Varying the {dimension} arm to '{arm}' will yield a "
                f"measurably higher success rate than the incumbent, testing "
                f"the stale belief: '{claim}'.")
    return (f"Varying the {dimension} arm to '{arm}' will yield a "
            f"measurably higher success rate than the incumbent.")


def _success_metric_for(dimension: str) -> str:
    return {
        "opener": "reply_rate",
        "subject": "reply_rate",
        "cta": "meeting_booked_rate",
        "landing": "meeting_booked_rate",
        "offer": "opportunity_created_rate",
        "call_script": "conversation_completed_rate",
        "cadence": "reply_rate_per_touch",
        "pricing_tier": "closed_won_rate",
    }.get(dimension, "conversion_rate")


def _pick_dimension_from_belief(claim_lower: str) -> str:
    """Best-effort dimension guess from a belief claim's language.

    Cause-and-effect only — no metaphors, no wisdom words. Falls back to
    ``opener`` (the lowest-risk dimension) when nothing keys.
    """
    tokens = [
        ("pricing", "pricing_tier"), ("price", "pricing_tier"), ("tier", "pricing_tier"),
        ("call", "call_script"), ("script", "call_script"),
        ("cadence", "cadence"), ("touch", "cadence"), ("frequency", "cadence"),
        ("subject", "subject"), ("headline", "subject"),
        ("cta", "cta"), ("button", "cta"),
        ("landing", "landing"), ("page", "landing"),
        ("offer", "offer"), ("bundle", "offer"), ("discount", "offer"),
        ("opener", "opener"), ("intro", "opener"), ("first line", "opener"),
    ]
    for key, dim in tokens:
        if key in claim_lower:
            return dim
    return "opener"


def _candidate_arm(dimension: str, covered_arms: set[str]) -> str:
    """A short, deterministic candidate arm slug not already in use."""
    base = {
        "opener": "curiosity-first",
        "subject": "specificity-first",
        "cta": "single-action",
        "landing": "value-tightened",
        "offer": "trial-scoped",
        "call_script": "diagnostic-open",
        "cadence": "3-touch-compressed",
        "pricing_tier": "annual-anchored",
    }.get(dimension, "variant-a")
    if base not in covered_arms:
        return base
    # Deterministic small numeric suffix so the slug is stable per proposal
    for suffix in ("b", "c", "d", "e"):
        candidate = f"{base}-{suffix}"
        if candidate not in covered_arms:
            return candidate
    return f"{base}-{uuid.uuid4().hex[:4]}"


def _stale_days(belief: Any) -> float:
    from datetime import datetime, timezone

    last = (getattr(belief, "last_verified", "") or "").strip()
    if not last:
        return 30.0  # never verified — treat as month-old
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return 30.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def _submit_approval(
    proposal: ExperimentProposal,
    *,
    risk_level: str,
) -> str:
    """HOTL approval via existing ADR-0019 machinery. Returns approval id.

    Never raises: on any failure the proposal stays ``pending`` and the
    operator will re-see it on the next consolidator pass.
    """
    try:
        from backend.common.approvals import create_approval

        row = create_approval(
            "experiment_proposal",
            {
                "proposal_id": proposal.proposal_id,
                "dimension": proposal.dimension,
                "hypothesis": proposal.hypothesis,
                "control_arm": proposal.control_arm,
                "candidate_arm": proposal.candidate_arm,
                "success_metric": proposal.success_metric,
                "min_sample_size": proposal.min_sample_size,
                "risk_score": proposal.risk_score,
                "expected_economic_impact": proposal.expected_economic_impact,
                "source_belief_ids": list(proposal.source_belief_ids),
            },
            risk_level=risk_level,
            ev_usd=proposal.expected_economic_impact,
        )
        return str(row.get("id", "") or "")
    except Exception as exc:  # noqa: BLE001 — approvals must not sink callers
        _LOG.warning("proposal approval submit failed: %s", exc)
        return ""


def _write_proposal(proposal: ExperimentProposal) -> None:
    try:
        _proposals_ledger().append(proposal.to_dict())
    except Exception as exc:  # noqa: BLE001 — replay ledger is best-effort
        _LOG.warning("proposal ledger append failed: %s", exc)


def _emit_proposal_event(proposal: ExperimentProposal) -> None:
    try:
        emit_business_event(
            "experiment.proposed",
            workcell="experiments",
            metadata={
                "proposal_id": proposal.proposal_id,
                "dimension": proposal.dimension,
                "status": proposal.status,
                "risk_score": proposal.risk_score,
                "expected_economic_impact": proposal.expected_economic_impact,
                "approval_id": proposal.approval_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.info("experiment.proposed event emit failed: %s", exc)


def propose_next_experiment(
    context: dict[str, Any] | None = None,
) -> ExperimentProposal | None:
    """Draft the next experiment from current observations.

    Inputs (READ ONLY):
      * ``belief_ledger.stale_beliefs()`` — beliefs due for re-verification.
      * ``belief_ledger.list_beliefs(status=ACTIVE)`` at confidence >= 0.85
        — settled claims we must NOT re-test.
      * consolidator's promoted patterns via ``GuidanceLedger.all_latest()``.
      * this registry's active experiments (to avoid duplicating coverage).

    Output: one :class:`ExperimentProposal` written to
    ``experiments/proposals.jsonl``. If its ``risk_score`` exceeds
    :data:`PROPOSAL_RISK_GATE` a ``create_approval`` row is filed with
    ADR-0019 severity + TTL and the proposal is left ``blocked_on_hotl``;
    otherwise it is promoted immediately to an active experiment via
    :func:`register_experiment`.

    Returns ``None`` if there is nothing to propose (empty observations, or
    every candidate collides with a settled belief / covered arm).
    """
    ctx = dict(context or {})
    min_impact = float(ctx.get("min_impact_usd", 0.0) or 0.0)

    stale = _read_stale_beliefs(min_impact=min_impact)
    settled = _read_settled_beliefs()
    covered = _current_experiment_arms()

    # Choose the highest-impact stale belief that is neither settled nor
    # already covered by an active experiment on the same arm.
    picked_belief: Any | None = None
    picked_dimension = ""
    picked_arm = ""
    for b in stale:
        claim_lower = (getattr(b, "claim", "") or "").strip().lower()
        if not claim_lower or claim_lower in settled:
            continue
        dim = str(ctx.get("dimension") or _pick_dimension_from_belief(claim_lower))
        if dim not in DIMENSIONS:
            continue
        arm = _candidate_arm(dim, covered.get(dim, set()))
        if not arm or arm in covered.get(dim, set()):
            continue
        picked_belief = b
        picked_dimension = dim
        picked_arm = arm
        break

    if picked_belief is None:
        # Nothing to propose — record a skipped-proposal marker so the
        # operator can see the loop ran and produced nothing.
        skipped = ExperimentProposal(
            proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
            dimension="",
            hypothesis="",
            control_arm="",
            candidate_arm="",
            success_metric="",
            status=PROPOSAL_STATUS_SKIPPED,
            created_at=iso_now(),
            metadata={"reason": "no_actionable_observation",
                      "stale_beliefs": len(stale),
                      "settled_beliefs": len(settled)},
        )
        _write_proposal(skipped)
        _emit_proposal_event(skipped)
        return None

    expected_impact = float(getattr(picked_belief, "economic_impact", 0.0) or 0.0)
    belief_tier = int(getattr(picked_belief, "tier", 2) or 2)
    stale_days = _stale_days(picked_belief)
    risk_score = _risk_score(
        dimension=picked_dimension,
        expected_impact=expected_impact,
        belief_tier=belief_tier,
        stale_days=stale_days,
    )

    control_arm = next(iter(covered.get(picked_dimension, set())), "incumbent")
    hypothesis = _hypothesis_from_belief(picked_belief, picked_dimension, picked_arm)

    guidance_ids = [
        g["recommendation_id"] for g in _read_promoted_patterns()
        if g.get("recommendation_id")
    ][:5]

    proposal = ExperimentProposal(
        proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
        dimension=picked_dimension,
        hypothesis=hypothesis,
        control_arm=control_arm,
        candidate_arm=picked_arm,
        success_metric=_success_metric_for(picked_dimension),
        min_sample_size=int(ctx.get("min_sample_size", DEFAULT_PROPOSAL_SAMPLE_SIZE)
                            or DEFAULT_PROPOSAL_SAMPLE_SIZE),
        risk_score=risk_score,
        expected_economic_impact=expected_impact,
        source_belief_ids=[str(getattr(picked_belief, "belief_id", "") or "")],
        source_guidance_ids=guidance_ids,
        created_at=iso_now(),
        metadata={
            "belief_tier": belief_tier,
            "stale_days": round(stale_days, 2),
        },
    )

    if risk_score > PROPOSAL_RISK_GATE:
        # Gate through HOTL before promoting to active. ADR-0019 severity
        # rides on ``risk_level`` -> ``severity_for`` -> emergency vs routine.
        risk_level = "critical" if risk_score >= 0.75 else "high"
        approval_id = _submit_approval(proposal, risk_level=risk_level)
        proposal.approval_id = approval_id
        proposal.status = PROPOSAL_STATUS_BLOCKED
        _write_proposal(proposal)
        _emit_proposal_event(proposal)
        return proposal

    # Low-risk: register the arms straight into the experiments registry.
    arms = [control_arm, picked_arm] if control_arm != picked_arm else [picked_arm, f"{picked_arm}-ctrl"]
    try:
        exp = register_experiment(
            dimension=picked_dimension,
            arms=arms,
            min_trials=proposal.min_sample_size,
            metadata={
                "proposal_id": proposal.proposal_id,
                "hypothesis": hypothesis,
                "success_metric": proposal.success_metric,
                "source_belief_ids": proposal.source_belief_ids,
            },
        )
        proposal.status = PROPOSAL_STATUS_PROMOTED
        proposal.metadata["experiment_id"] = exp.experiment_id
    except Exception as exc:  # noqa: BLE001 — persist proposal even if registration fails
        _LOG.warning("propose_next_experiment: registration failed (%s); "
                     "leaving proposal pending", exc)

    _write_proposal(proposal)
    _emit_proposal_event(proposal)
    return proposal


def list_proposals(*, status: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    """Recent proposals (oldest-first). Fail-open to []."""
    try:
        rows = _proposals_ledger().tail(limit=limit)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("proposals tail failed: %s", exc)
        return []
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows


__all__ = [
    "ARM_NAMESPACE_SEP",
    "DIMENSIONS",
    "DEFAULT_MIN_TRIALS",
    "DEFAULT_PROPOSAL_SAMPLE_SIZE",
    "PROPOSAL_RISK_GATE",
    "PROPOSAL_STATUS_PENDING",
    "PROPOSAL_STATUS_BLOCKED",
    "PROPOSAL_STATUS_PROMOTED",
    "PROPOSAL_STATUS_SKIPPED",
    "Experiment",
    "ExperimentProposal",
    "build_experiment_arm_id",
    "register_experiment",
    "get_experiment",
    "list_experiments",
    "save_experiment",
    "assign",
    "record_result",
    "arm_stats",
    "assignments_tail",
    "propose_next_experiment",
    "list_proposals",
]
