"""PDC Composite — Periodic Defensive Check, composite run.

A PDC composite run aggregates Samus's depth-layer gates into one signed
finding record. The output is the `pdc_finding` referenced by
`Darwin/axioms/adversarial_actor.spec.yaml :: adjustment_policy.requires`
and by `inviolable_axioms.yaml` adjustment workflow.

Gates aggregated (in order):

  1. EFH — inviolable-axiom violations (`backend.governance.efh_evaluator`)
  2. Confusion meter — windowed score over emitted ConfusionEvents
     (`backend.observability.confusion_meter`)
  3. Elegance scorer — plan-complexity-vs-impact
     (`backend.governance.elegance_scorer`)
  4. Adversarial-actor classifier — does the counterparty in the proposed
     action match any classification criterion from
     `axioms/adversarial_actor.spec.yaml`?

The composite verdict is one of:

  PASS         — no inviolable veto, confusion below threshold, elegance >= floor
  REVIEW       — soft signals present (confusion >= 0.4 OR elegance < 0.4)
  BLOCK        — EFH inviolable veto present, OR adversarial classification
                 against an internal target (axiom.no_adversarial_host_action)
  ESCALATE     — adversarial classification against an external party (the
                 adversarial-defense exception in inviolable_axioms applies;
                 the finding routes to /gate before any defensive action)

Pure-stdlib + pyyaml. No new env vars. The finding is persisted under
`Samus/state/pdc/findings/<finding_id>.yaml` and the finding_id is returned
so the adjustment_policy gate can quote it.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.common.state_paths import state_path
from backend.governance.efh_evaluator import EthicalFailureHandler
from backend.governance.elegance_scorer import ElegancePlan, score_elegance
from backend.observability.confusion_meter import ConfusionScore, compute_confusion_score

_SAMUS_ROOT = Path(__file__).resolve().parents[2]
_AXIOMS_DIR = _SAMUS_ROOT / "axioms"
# Findings persist on the writable data volume in-container (the image root
# /opt/samus is read-only). See backend/common/state_paths.py.
_FINDINGS_SINK = state_path("pdc", "findings")

# Thresholds for the composite verdict.
_CONFUSION_REVIEW_FLOOR = 0.4
_CONFUSION_BLOCK_FLOOR = 0.8
_ELEGANCE_REVIEW_FLOOR = 0.4
# A passing taste audit below this score still trips a soft REVIEW signal when
# the taste gate is armed. Overridable per call from settings.taste_review_floor.
_TASTE_REVIEW_FLOOR_DEFAULT = 0.6

# Verdict ordering — used to pick the most severe of multiple signals.
_VERDICT_ORDER = ("PASS", "REVIEW", "ESCALATE", "BLOCK")


@dataclass
class PDCFinding:
    """A single PDC composite run.

    Append-only — once persisted, the YAML is the audit record. The
    finding_id can be quoted as the `documented_basis: pdc_finding` in
    adversarial_actor.spec.yaml adjustment_policy.
    """

    finding_id: str
    ts: str
    verdict: str
    proposed_action: dict[str, Any]
    efh_veto: dict[str, Any] | None
    confusion: dict[str, Any]
    elegance: dict[str, Any]
    adversarial: dict[str, Any]
    rationale: list[str] = field(default_factory=list)
    # Gate 5 — deliverable design-taste (anti-slop). Inert ({"evaluated": False})
    # unless a taste_artifact is supplied to run_pdc; see backend.taste.audit.
    taste: dict[str, Any] = field(default_factory=lambda: {"evaluated": False})

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "ts": self.ts,
            "verdict": self.verdict,
            "proposed_action": self.proposed_action,
            "efh_veto": self.efh_veto,
            "confusion": self.confusion,
            "elegance": self.elegance,
            "adversarial": self.adversarial,
            "taste": self.taste,
            "rationale": list(self.rationale),
        }


def _max_verdict(*verdicts: str) -> str:
    return max(verdicts, key=lambda v: _VERDICT_ORDER.index(v))


def _load_adversarial_spec(axioms_dir: Path) -> dict[str, Any] | None:
    p = axioms_dir / "adversarial_actor.spec.yaml"
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _classify_adversarial(
    proposed_action: dict[str, Any],
    spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Match the proposed action's `target` against the classification criteria.

    A target classifies adversarial when ANY criterion matches. Self-asserted
    classifications without evidence_source are NOT honored (see anti_abuse
    provision `classification_evidence_must_be_loggable` in the spec).

    Returns a dict — never None — so the finding always has an `adversarial`
    section even when the spec is missing.
    """
    out: dict[str, Any] = {
        "classified": False,
        "criterion_id": None,
        "target": proposed_action.get("target"),
        "target_is_internal": bool(proposed_action.get("target_is_internal", False)),
        "evidence_refs": [],
        "spec_loaded": spec is not None,
    }
    if spec is None:
        return out

    target = proposed_action.get("target") or {}
    evidence_refs = list(target.get("evidence_refs") or [])
    declared_match = target.get("matches_criterion")

    criteria = {c["id"]: c for c in spec.get("classification_criteria", [])}

    # Honor an explicit criterion declaration only if the criterion exists
    # AND at least one evidence_ref accompanies it (or it's alex_manual).
    if declared_match in criteria:
        crit = criteria[declared_match]
        if declared_match == "alex_manual_classification":
            if target.get("classified_by") == "Alex Hartman" and target.get("reason"):
                out.update(
                    {
                        "classified": True,
                        "criterion_id": declared_match,
                        "evidence_refs": evidence_refs,
                    }
                )
                return out
        elif evidence_refs:
            allowed = set(crit.get("evidence_sources") or []) or None
            if allowed is None or any(ref.split(":", 1)[0] in allowed for ref in evidence_refs):
                out.update(
                    {
                        "classified": True,
                        "criterion_id": declared_match,
                        "evidence_refs": evidence_refs,
                    }
                )
                return out

    # Behavioral-pattern path: host_sentinel_flag with at least one
    # listed behavioral_patterns_that_qualify present in target.patterns.
    if "host_sentinel_flag" in criteria:
        crit = criteria["host_sentinel_flag"]
        allowed_patterns = set(crit.get("behavioral_patterns_that_qualify") or [])
        observed = set(target.get("patterns") or [])
        if allowed_patterns and observed & allowed_patterns and evidence_refs:
            out.update(
                {
                    "classified": True,
                    "criterion_id": "host_sentinel_flag",
                    "evidence_refs": evidence_refs,
                    "matched_patterns": sorted(observed & allowed_patterns),
                }
            )
            return out

    return out


def _persist(finding: PDCFinding, sink: Path | None = None) -> Path:
    sink = sink or _FINDINGS_SINK
    sink.mkdir(parents=True, exist_ok=True)
    path = sink / f"{finding.finding_id}.yaml"
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(finding.to_dict(), fh, sort_keys=False)
    return path


def run_pdc(
    proposed_action: dict[str, Any],
    *,
    plan: dict[str, Any] | None = None,
    axioms_dir: Path | None = None,
    confusion_window_seconds: int = 3600,
    confusion_events: list[dict[str, Any]] | None = None,
    persist: bool = True,
    sink: Path | None = None,
    taste_artifact: dict[str, Any] | None = None,
    taste_gate: bool = False,
    taste_review_floor: float = _TASTE_REVIEW_FLOOR_DEFAULT,
) -> PDCFinding:
    """Run one PDC composite. Returns a PDCFinding.

    Args:
        proposed_action: the action the caller is about to commit. Forwarded
            to EFH; its `target` field is used by the adversarial classifier.
        plan: optional plan dict for the elegance scorer. If omitted a default
            (zero-impact) plan is scored — produces F-grade elegance + a
            rationale line so the absence is auditable.
        axioms_dir: override the default `Samus/axioms/`.
        confusion_window_seconds: trailing window over ConfusionEvents.
        confusion_events: pre-loaded events for tests; else read from JSONL.
        persist: write the finding YAML under `state/pdc/findings/`.
        sink: explicit findings dir for tests.
        taste_artifact: optional rendered deliverable dict (see
            backend.taste.audit.audit_deliverable). When None (the default and
            every legacy caller) the taste section stays inert and the verdict
            is byte-for-byte the four-gate result.
        taste_gate: when True AND a taste_artifact is supplied, the taste
            verdict folds into the composite as a soft REVIEW signal. Never
            BLOCKs on taste alone.
        taste_review_floor: a passing audit below this score still trips REVIEW.

    Returns:
        PDCFinding. Composite `verdict` is the most-severe of the gates.
    """
    axioms_dir = axioms_dir or _AXIOMS_DIR

    # Gate 1 — EFH (inviolable axioms + consent).
    efh = EthicalFailureHandler(axioms_dir=axioms_dir)
    veto = efh.evaluate(proposed_action)

    # Gate 2 — Confusion meter.
    confusion: ConfusionScore = compute_confusion_score(
        window_seconds=confusion_window_seconds,
        events=confusion_events,
    )

    # Gate 3 — Elegance.
    elegance: ElegancePlan = score_elegance(plan or {"projected_impact": 0.0})

    # Gate 4 — Adversarial-actor classification.
    spec = _load_adversarial_spec(axioms_dir)
    adversarial = _classify_adversarial(proposed_action, spec)

    # Composite verdict.
    verdict = "PASS"
    rationale: list[str] = []

    if veto is not None:
        verdict = _max_verdict(verdict, "BLOCK")
        rationale.append(f"EFH inviolable veto on {','.join(veto['inviolable_axioms_breached'])}")

    if confusion.score >= _CONFUSION_BLOCK_FLOOR:
        verdict = _max_verdict(verdict, "BLOCK")
        rationale.append(f"confusion saturated ({confusion.score:.2f} >= {_CONFUSION_BLOCK_FLOOR})")
    elif confusion.score >= _CONFUSION_REVIEW_FLOOR:
        verdict = _max_verdict(verdict, "REVIEW")
        rationale.append(f"confusion elevated ({confusion.score:.2f} >= {_CONFUSION_REVIEW_FLOOR})")

    if elegance.score < _ELEGANCE_REVIEW_FLOOR:
        verdict = _max_verdict(verdict, "REVIEW")
        rationale.append(
            f"elegance below review floor ({elegance.score:.2f} < {_ELEGANCE_REVIEW_FLOOR})"
        )

    if adversarial["classified"]:
        if adversarial["target_is_internal"]:
            # Adversarial action against the host / peer agent / Alex's data —
            # categorically blocked by axiom.inviolable.no_adversarial_host_action.
            verdict = _max_verdict(verdict, "BLOCK")
            rationale.append("adversarial target is internal — no_adversarial_host_action applies")
        else:
            # External adversary — defense exception MAY apply but routes to /gate.
            verdict = _max_verdict(verdict, "ESCALATE")
            rationale.append(
                f"adversarial classification via {adversarial['criterion_id']} — routes to /gate"
            )

    # Gate 5 — Taste (deliverable design-quality). Inert unless a taste_artifact
    # is supplied. Soft signal only: contributes at most REVIEW, mirroring the
    # elegance floor; never BLOCKs (a slop deliverable is a quality defect, not
    # an inviolable-axiom breach).
    taste: dict[str, Any] = {"evaluated": False}
    if taste_artifact is not None:
        from backend.taste.audit import audit_deliverable

        taste_result = audit_deliverable(taste_artifact)
        taste = taste_result.to_dict()
        taste["evaluated"] = True
        if taste_gate:
            if not taste_result.passed:
                verdict = _max_verdict(verdict, "REVIEW")
                rationale.append(
                    f"taste audit failed (grade {taste_result.grade}, "
                    f"{taste_result.fail_count} hard violation(s))"
                )
            elif taste_result.score < taste_review_floor:
                verdict = _max_verdict(verdict, "REVIEW")
                rationale.append(
                    f"taste below review floor ({taste_result.score:.2f} < {taste_review_floor})"
                )

    finding = PDCFinding(
        finding_id=str(uuid.uuid4()),
        ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        verdict=verdict,
        proposed_action={
            "kind": proposed_action.get("kind", "unspecified"),
            "proposing_agent": proposed_action.get("proposing_agent", "samus"),
            "target": proposed_action.get("target"),
        },
        efh_veto=veto,
        confusion=confusion.to_dict(),
        elegance=elegance.to_dict(),
        adversarial=adversarial,
        taste=taste,
        rationale=rationale,
    )

    if persist:
        _persist(finding, sink=sink)

    # Best-effort publish to the cross-agent Quorum Hub. Fails open — a hub
    # outage or SAMUS_QUORUM_PUBLISH_ENABLED=off short-circuits silently.
    # PASS/REVIEW stay local; only BLOCK/ESCALATE escalate to peer agents.
    if verdict in {"BLOCK", "ESCALATE"}:
        try:
            from backend.common.quorum_publisher import publish_pdc_finding

            publish_pdc_finding(finding)
        except Exception:  # noqa: BLE001
            pass

    return finding
