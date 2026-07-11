"""Semantic publishers — convert Samus depth-layer records into governance_publish.

The hub's `governance_publish` schema expects a uniform tuple of:
    caller, action, risk_score (0..1), approved, approval_score (0..1),
    threshold (0..1), votes (list of {voter, vote, weight}), reason

This module is the single chokepoint that maps EFH vetoes + PDC findings
onto that shape. All publishes are best-effort and fail open — a hub
outage never blocks the caller.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common.quorum_client import get_quorum_client

_LOG = logging.getLogger("samus.quorum_publisher")

# Risk scoring for verdicts. PDC verdicts map onto a 0..1 risk axis.
_PDC_RISK: dict[str, float] = {
    "PASS": 0.0,
    "REVIEW": 0.3,
    "ESCALATE": 0.7,
    "BLOCK": 0.95,
}

# PDC verdicts that warrant a hub publish. PASS and REVIEW stay local —
# the hub is for cross-agent escalations, not for hum-drum review queues.
_PDC_PUBLISH_VERDICTS = {"BLOCK", "ESCALATE"}

# Standard threshold the hub records for context. PDC verdicts are pre-computed
# composites — the hub records them as decisions rather than re-voting them.
_DEFAULT_THRESHOLD = 0.5


def publish_efh_veto(veto: dict[str, Any]) -> bool:
    """Publish one EthicalReviewVeto to the quorum hub.

    Veto shape (from EthicalFailureHandler.evaluate):
        veto_id, ts, proposing_agent, proposed_action, projected_*_outcome,
        inviolable_axioms_breached, asymmetry_score, escalation, ...

    Maps to governance_publish:
        caller = proposing_agent (default 'samus')
        action = 'efh_veto'
        risk_score = 1.0 (every inviolable veto is maximum risk)
        approved = False (vetoes are denials)
        approval_score = 0.0
        threshold = 1.0 (no vote ever overrides an inviolable axiom)
        votes = [{voter: 'efh', vote: 'VETO', weight: 1.0}]
        reason = "veto={veto_id}; axioms={ax1,ax2}"
    """
    if not isinstance(veto, dict) or not veto.get("veto_id"):
        return False
    breached = veto.get("inviolable_axioms_breached") or []
    reason = (
        f"veto={veto['veto_id']}; "
        f"axioms={','.join(breached) if breached else 'none'}; "
        f"escalation={veto.get('escalation', 'gate_only')}"
    )
    try:
        return get_quorum_client().publish(
            caller=str(veto.get("proposing_agent", "samus")),
            action="efh_veto",
            risk_score=1.0,
            approved=False,
            approval_score=0.0,
            threshold=1.0,
            votes=[{"voter": "efh", "vote": "VETO", "weight": 1.0}],
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("publish_efh_veto failed: %s", exc)
        return False


def publish_pdc_finding(finding: Any) -> bool:
    """Publish one PDC composite finding to the quorum hub.

    Accepts either a `PDCFinding` dataclass or its `.to_dict()` form. Only
    fires for BLOCK / ESCALATE verdicts — PASS/REVIEW stay local.

    Maps to governance_publish:
        caller = proposed_action.proposing_agent (default 'samus')
        action = 'pdc_finding'
        risk_score = _PDC_RISK[verdict]
        approved = verdict == 'PASS'
        approval_score = 1 - risk_score
        threshold = 0.5
        votes = one vote per signal that fired (efh / confusion / elegance / adversarial)
        reason = "finding={finding_id}; verdict={verdict}; {top rationale line}"
    """
    record = finding.to_dict() if hasattr(finding, "to_dict") else finding
    if not isinstance(record, dict):
        return False
    verdict = str(record.get("verdict") or "").upper()
    if verdict not in _PDC_PUBLISH_VERDICTS:
        return False

    risk = _PDC_RISK.get(verdict, 0.5)
    proposed = record.get("proposed_action") or {}
    caller = str(proposed.get("proposing_agent", "samus"))
    rationale = record.get("rationale") or []
    headline = rationale[0] if rationale else f"verdict={verdict}"

    votes: list[dict[str, Any]] = []
    if record.get("efh_veto"):
        votes.append({"voter": "efh", "vote": "VETO", "weight": 1.0})
    confusion = record.get("confusion") or {}
    if confusion.get("score", 0.0) >= 0.4:
        votes.append({
            "voter": "confusion_meter",
            "vote": "REVIEW" if confusion["score"] < 0.8 else "BLOCK",
            "weight": float(confusion["score"]),
        })
    elegance = record.get("elegance") or {}
    if elegance.get("score", 1.0) < 0.4:
        votes.append({
            "voter": "elegance_scorer",
            "vote": "REVIEW",
            "weight": 1.0 - float(elegance["score"]),
        })
    adversarial = record.get("adversarial") or {}
    if adversarial.get("classified"):
        votes.append({
            "voter": "adversarial_actor",
            "vote": "BLOCK" if adversarial.get("target_is_internal") else "ESCALATE",
            "weight": 1.0,
        })
    if not votes:
        # Defensive: every BLOCK/ESCALATE should have at least one contributing
        # signal, but if the upstream record is malformed, still publish with a
        # synthetic vote so the hub sees the verdict.
        votes.append({"voter": "pdc_composite", "vote": verdict, "weight": 1.0})

    reason = (
        f"finding={record.get('finding_id', '<unknown>')}; "
        f"verdict={verdict}; {headline}"
    )
    try:
        return get_quorum_client().publish(
            caller=caller,
            action="pdc_finding",
            risk_score=risk,
            approved=False,
            approval_score=max(0.0, 1.0 - risk),
            threshold=_DEFAULT_THRESHOLD,
            votes=votes,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("publish_pdc_finding failed: %s", exc)
        return False


# Parliament outcomes that warrant a hub publish. Approvals on borderline
# proposals stay local — the hub is the cross-agent escalation surface, so we
# only fan out the verdicts a peer agent would want to react to: a VETO (the
# parliament advises halt + escalate) or an ABSTAIN (advisory deadlock the
# operator/Darwin should arbitrate). Clean APPROVE verdicts do not escalate.
_PARLIAMENT_PUBLISH_OUTCOMES = {"veto", "abstain"}

# Map a parliament outcome onto the hub's 0..1 risk axis.
_PARLIAMENT_RISK: dict[str, float] = {
    "approve": 0.1,
    "abstain": 0.5,
    "veto": 0.9,
}


def publish_parliament_verdict(
    verdict: Any,
    *,
    action: str,
    risk_score: float | None = None,
    caller: str = "samus",
) -> bool:
    """Publish one GovernanceParliament verdict to the quorum hub.

    Accepts a ``ParliamentVerdict`` dataclass (from
    ``backend.common.governance_parliament``) or any object exposing the same
    attribute surface. Only VETO / ABSTAIN outcomes fan out — clean APPROVE
    verdicts stay local (see ``_PARLIAMENT_PUBLISH_OUTCOMES``).

    Maps to governance_publish:
        caller         = caller (default 'samus')
        action         = 'parliament_{action}' (e.g. parliament_mutation_apply)
        risk_score     = the proposal's own risk_score if supplied, else the
                         outcome-derived risk from _PARLIAMENT_RISK
        approved       = outcome == 'approve'
        approval_score = verdict.approval_ratio
        threshold      = verdict.threshold
        votes          = one entry per agent in verdict.vote_detail
        reason         = "parliament={outcome}; {verdict.reason}"

    Best-effort + fail-open: returns False on any error, when the outcome
    doesn't warrant a publish, or when publishing is disabled (the client's
    own SAMUS_QUORUM_PUBLISH_ENABLED gate). Never raises.
    """
    try:
        outcome = str(getattr(verdict, "outcome", "")).lower()
        if outcome not in _PARLIAMENT_PUBLISH_OUTCOMES:
            return False

        detail = getattr(verdict, "vote_detail", None) or []
        votes: list[dict[str, Any]] = [
            {
                "voter": str(entry.get("agent", "?")),
                "vote": str(entry.get("vote", "abstain")).upper(),
                "weight": float(entry.get("weight", 0.0)),
            }
            for entry in detail
            if isinstance(entry, dict)
        ]
        if not votes:
            # Defensive: a no-eligible-agents / quorum veto carries no per-agent
            # detail. Still publish with a synthetic vote so the hub records the
            # escalation rather than dropping it silently.
            votes.append({"voter": "parliament", "vote": outcome.upper(), "weight": 1.0})

        risk = (
            float(risk_score)
            if risk_score is not None
            else _PARLIAMENT_RISK.get(outcome, 0.5)
        )
        reason = f"parliament={outcome}; {getattr(verdict, 'reason', '')}"

        return get_quorum_client().publish(
            caller=caller,
            action=f"parliament_{action}" if action else "parliament_vote",
            risk_score=risk,
            approved=(outcome == "approve"),
            approval_score=float(getattr(verdict, "approval_ratio", 0.0)),
            threshold=float(getattr(verdict, "threshold", _DEFAULT_THRESHOLD)),
            votes=votes,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("publish_parliament_verdict failed: %s", exc)
        return False


__all__ = ["publish_efh_veto", "publish_pdc_finding", "publish_parliament_verdict"]
