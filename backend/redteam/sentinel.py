"""Samus-Red sentinel -- the nightly adversarial resilience pass.

Orchestrates one Samus-Red run:

  1. Sense the live defensive posture (fail-soft).
  2. Run every deterministic attack probe over it.
  3. Score resilience + antifragility vs the previous run (append-only ledger).
  4. Blue-consumption: file each BREACH into the guidance ledger as an
     ACCEPTED, operator/owner-routed recommendation, and CLOSE a prior breach
     that is now contained (record_outcome -> COMPLETED). Guidance rows are
     keyed by a stable ``redteam-<probe>`` id, so a persisting breach is not
     re-filed every night and a resolved one is scored as a Blue win.

Zero LLM. Deterministic given a fixed posture. Default-on: wired as the fifth
stage of the nightly consolidation (see :mod:`backend.cognitive.consolidator`).
A guidance breach becomes visible to the REASON stage (active_guidance_context)
and the morning brief on the next cycle -- that is how Red's findings become
Blue's remediation work.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from backend.common.dates import iso_now
from backend.common.persistence import JsonlLedger
from backend.common.state_paths import state_path

from .models import KIND, ProbeOutcome, ResilienceReport, build_report
from .probes import run_probes
from .sensors import sense_posture

log = logging.getLogger("samus.redteam.sentinel")

_LEDGER_PARTS = ("redteam", "redteam_ledger.jsonl")


def _ledger_path() -> str:
    return str(state_path(*_LEDGER_PARTS))


def _prior_breaches(ledger: Any) -> List[str]:
    """Breached probe ids from the most recent recorded report (or [])."""
    try:
        rows = ledger.tail(50)
    except Exception as exc:  # noqa: BLE001
        log.warning("redteam: prior-breach read failed: %s", exc)
        return []
    reports = [r for r in rows if isinstance(r, dict) and r.get("kind") == KIND]
    if not reports:
        return []
    return [str(b) for b in (reports[-1].get("breaches") or [])]


def _tier_for_severity(severity: int) -> int:
    # GuidanceTier: 1=CRITICAL, 2=HIGH_VALUE, 3=INFORMATIONAL -- matches our
    # severity banding 1=critical .. 3=moderate directly.
    return max(1, min(3, int(severity)))


def _sync_guidance(guidance_ledger: Any, report: ResilienceReport, now: str) -> dict:
    """File new breaches; close breaches now contained. Returns counts + ids."""
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.guidance_models import (
        GuidanceCategory,
        GuidanceRecord,
        GuidanceStatus,
    )

    gl = guidance_ledger if guidance_ledger is not None else GuidanceLedger()
    opened: List[str] = []
    resolved: List[str] = []

    for r in report.results:
        rid = f"redteam-{r.probe}"
        tag = f"redteam:{r.probe}"
        existing = gl.get(rid)
        is_ours_open = (
            existing is not None and not existing.is_terminal() and existing.source_question == tag
        )

        if r.breached:
            if is_ours_open:
                continue  # already on Blue's queue -- do not re-file every night
            base_rev = (existing.revision + 1) if existing is not None else 0
            rec = GuidanceRecord(
                recommendation_id=rid,
                briefing_id=f"redteam-{report.day}",
                ts=now,
                updated_ts=now,
                recommendation=(r.remediation or r.title),
                rationale=f"Samus-Red probe '{r.probe}' BREACHED: {r.evidence}",
                category=GuidanceCategory.RISK.value,
                tier=_tier_for_severity(r.severity),
                feasibility="high",
                expected_impact="high",
                risk_level="high" if r.severity == 1 else "medium",
                action_plan=[r.remediation] if r.remediation else [],
                owner=r.owner,
                acceptance_hint="accept",
                status=GuidanceStatus.ACCEPTED.value,
                revision=base_rev,
                source_question=tag,
            )
            gl.append(rec)
            opened.append(rid)
        elif is_ours_open and r.outcome == ProbeOutcome.CONTAINED.value:
            # A breach we filed is now contained on re-test -- Blue won. Close it
            # with an effectiveness score so the win becomes durable memory.
            gl.record_outcome(
                rid,
                outcome="contained on nightly re-test -- defense held",
                impact_actual="high",
                success_score=1.0,
                lessons_learned=f"Samus-Red: {r.title} now contained",
            )
            resolved.append(rid)

    return {"opened": opened, "resolved": resolved}


def run_redteam_pass(
    day: str = "",
    *,
    posture: Optional[Any] = None,
    redteam_ledger: Optional[Any] = None,
    guidance_ledger: Optional[Any] = None,
    emit_guidance: bool = True,
) -> dict[str, Any]:
    """Run one Samus-Red pass. Never raises -- every side effect is best-effort.

    ``posture`` / ``redteam_ledger`` / ``guidance_ledger`` are injectable for
    tests; production defaults sense the live system and use the JSONL ledgers.
    """
    now = iso_now()
    d = (day or "").strip() or now[:10]

    current_posture = posture if posture is not None else sense_posture()
    results = run_probes(current_posture)

    ledger = redteam_ledger if redteam_ledger is not None else JsonlLedger(_ledger_path())
    prior = _prior_breaches(ledger)
    report = build_report(d, now, results, prior)

    try:
        ledger.append(report.to_dict())
    except Exception as exc:  # noqa: BLE001 -- persistence is best-effort
        log.warning("redteam: ledger append failed: %s", exc)

    guidance = {"opened": [], "resolved": []}
    if emit_guidance:
        try:
            guidance = _sync_guidance(guidance_ledger, report, now)
        except Exception as exc:  # noqa: BLE001 -- guidance sync never sinks the pass
            log.warning("redteam: guidance sync failed: %s", exc)

    log.info(
        "redteam %s: score=%.3f breaches=%s antifragility=%+d",
        d,
        report.resilience_score,
        report.breaches,
        report.antifragility_delta,
    )
    return {
        "day": d,
        "resilience_score": report.resilience_score,
        "probes": len(results),
        "breaches": report.breaches,
        "hardened": report.hardened,
        "regressed": report.regressed,
        "antifragility_delta": report.antifragility_delta,
        "guidance_opened": guidance["opened"],
        "guidance_resolved": guidance["resolved"],
    }


__all__ = ["run_redteam_pass"]
