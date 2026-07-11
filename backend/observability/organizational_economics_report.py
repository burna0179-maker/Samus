"""Organizational-economics report - where is organizational energy wasted?

A read-only joined report across four existing Samus surfaces:

* ``backend.governance.org_debt`` - per-workcell debt (karma + efficiency +
  circuit trips), a proxy for coordination burden and how unevenly load is
  distributed across workcells.
* ``backend.strategy.regret_engine`` - cumulative bandit regret + regret-per-
  token, a proxy for signal wasted in the exploration/exploitation trade-off.
* ``backend.strategy.saturation_monitor`` - per-vertical saturation risk, a
  proxy for cognitive overhead (over-explored regions yield diminishing
  returns yet still cost attention).
* ``backend.common.approvals`` - the HOTL queue: pending count + oldest age
  proxy decision latency; the pending/(pending+expired) split proxies
  approval friction.

The report exposes six metrics named in Concept 4 of the Samus Assimilation
Plan (docs/Samus_Assimilation_Plan_Institutional_Cognition_2026-07-06.md):

    coordination_cost, decision_latency, context_switching,
    cognitive_overhead, approval_friction, communication_entropy

Each metric row cites the exact source module it was derived from so the
operator can trace a number back to its origin. Read-only by construction -
no source module is written and no ledger is mutated. Fail-soft - a missing
or unreadable source degrades to a neutral value tagged ``sources_missing``
rather than raising, matching the morning brief's never-break-the-brief
contract used elsewhere in ``backend.observability``.

Placement lives alongside ``production_health.py`` in
``backend/observability/`` so the daily report surface stays in one place.
No new top-level package; no source modules are refactored.

Wire: enabled by default; kill-switch ``SAMUS_ORG_ECONOMICS_REPORT_ENABLED``
(set 0/false/off to silence, matching production_health's convention).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_LOG = logging.getLogger("samus.observability.organizational_economics")

__all__ = [
    "OrganizationalEconomicsReport",
    "MetricRow",
    "compute_organizational_economics",
    "main",
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Reference token spend baseline for regret-per-token when per-arm cost
# telemetry is absent (regret_engine.DEFAULT_TOKEN_SPEND). Keeping this local
# means the report is well-defined even before that telemetry lands.
_REGRET_TOKEN_BASELINE = 1.0

# Number of most-indebted workcells to surface as the "top waste vector" for
# coordination cost. Kept small so the report is scannable in a morning brief.
_TOP_WORKCELL_LIMIT = 3


def _enabled() -> bool:
    return os.getenv("SAMUS_ORG_ECONOMICS_REPORT_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class MetricRow:
    """One metric of the six named in Concept 4.

    ``value`` is the primary scalar. ``unit`` describes it in words. ``source``
    is the fully-qualified module the value was derived from (so a caller can
    grep back to origin). ``detail`` is a one-line human-readable summary.
    ``sources_missing`` flips to True when the underlying source was
    unavailable and a neutral fallback was used.
    """
    name: str
    value: float
    unit: str
    source: str
    detail: str
    sources_missing: bool = False


@dataclass
class OrganizationalEconomicsReport:
    metrics: list[MetricRow] = field(default_factory=list)
    generated_ts: float = 0.0
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Structured dict view - the canonical machine-readable shape."""
        return {
            "generated_ts": self.generated_ts,
            "enabled": self.enabled,
            "metrics": {
                m.name: {
                    "value": m.value,
                    "unit": m.unit,
                    "source": m.source,
                    "detail": m.detail,
                    "sources_missing": m.sources_missing,
                }
                for m in self.metrics
            },
        }

    def summary_line(self) -> str:
        if not self.enabled:
            return "organizational economics: disabled"
        if not self.metrics:
            return "organizational economics: no metrics"
        degraded = sum(1 for m in self.metrics if m.sources_missing)
        tag = f" ({degraded} degraded)" if degraded else ""
        return f"organizational economics: {len(self.metrics)} metrics{tag}"


# ---------------------------------------------------------------------------
# Source readers - each returns a MetricRow; fail-soft neutral on any error
# ---------------------------------------------------------------------------

def _coordination_cost(org_debt_reader: Optional[Callable[[], dict[str, Any]]]) -> MetricRow:
    """Total organizational debt as a coordination-cost proxy.

    High total org_debt across workcells means many units are simultaneously
    unhealthy - the operator (or portfolio_controller as department manager)
    must spend attention coordinating recovery, which is coordination cost.
    """
    name = "coordination_cost"
    source = "backend.governance.org_debt.org_debt_report"
    try:
        reader = org_debt_reader
        if reader is None:
            from backend.governance.org_debt import org_debt_report
            reader = org_debt_report
        report = reader() or {}
        total = float(report.get("total_org_debt") or 0.0)
        worst = str(report.get("worst") or "")
        workcells = report.get("workcells") or []
        top = ", ".join(
            f"{row.get('workcell')}={row.get('org_debt')}"
            for row in workcells[:_TOP_WORKCELL_LIMIT]
        )
        detail = f"total org_debt {total:.3f}; worst={worst or 'n/a'}; top: {top or 'n/a'}"
        return MetricRow(name, total, "sum_org_debt", source, detail)
    except Exception as exc:  # noqa: BLE001 - fail-soft neutral
        _LOG.debug("coordination_cost read failed: %s", exc)
        return MetricRow(name, 0.0, "sum_org_debt", source, f"org_debt unavailable: {exc}", sources_missing=True)


def _decision_latency(approvals_reader: Optional[Callable[[], list[dict[str, Any]]]], now_ts: float) -> MetricRow:
    """Median age of pending HOTL approvals - how long decisions wait.

    Long-pending approvals stall every gated action downstream (stake gate,
    entropy countermeasures, HIGH/CRITICAL classifications). Median age of
    the pending queue is a good latency proxy that doesn't blow up on a
    single outlier.
    """
    name = "decision_latency"
    source = "backend.common.approvals.list_approvals(status=pending)"
    try:
        reader = approvals_reader
        if reader is None:
            from backend.common.approvals import STATUS_PENDING, list_approvals

            def reader() -> list[dict[str, Any]]:
                return list_approvals(status=STATUS_PENDING, limit=200)
        rows = reader() or []
        ages_s: list[float] = []
        for row in rows:
            created = str(row.get("created_at") or "")
            if not created:
                continue
            epoch = _iso_to_epoch(created)
            if epoch <= 0:
                continue
            ages_s.append(max(0.0, now_ts - epoch))
        if not ages_s:
            return MetricRow(name, 0.0, "hours_median", source, "no pending approvals")
        ages_s.sort()
        median_s = ages_s[len(ages_s) // 2]
        median_h = median_s / 3600.0
        detail = f"{len(ages_s)} pending, median age {median_h:.2f}h, oldest {ages_s[-1] / 3600.0:.2f}h"
        return MetricRow(name, round(median_h, 4), "hours_median", source, detail)
    except Exception as exc:  # noqa: BLE001 - fail-soft neutral
        _LOG.debug("decision_latency read failed: %s", exc)
        return MetricRow(name, 0.0, "hours_median", source, f"approvals unavailable: {exc}", sources_missing=True)


def _context_switching(org_debt_reader: Optional[Callable[[], dict[str, Any]]]) -> MetricRow:
    """Dispersion of per-workcell debt as a context-switching proxy.

    When every workcell has similar debt, attention flows evenly; when debt
    is unevenly spread across workcells, the operator context-switches
    between healthy and struggling units. Population std-dev of per-workcell
    debt is the dispersion signal, and it uses the SAME org_debt read that
    coordination_cost uses (single source, two metrics).
    """
    name = "context_switching"
    source = "backend.governance.org_debt.org_debt_report[workcells]"
    try:
        reader = org_debt_reader
        if reader is None:
            from backend.governance.org_debt import org_debt_report
            reader = org_debt_report
        report = reader() or {}
        rows = report.get("workcells") or []
        debts = [float(r.get("org_debt") or 0.0) for r in rows]
        if not debts:
            return MetricRow(name, 0.0, "stddev_org_debt", source, "no workcells reported")
        mean = sum(debts) / len(debts)
        variance = sum((d - mean) ** 2 for d in debts) / len(debts)
        stddev = variance ** 0.5
        detail = f"{len(debts)} workcells, mean debt {mean:.3f}, stddev {stddev:.3f}"
        return MetricRow(name, round(stddev, 4), "stddev_org_debt", source, detail)
    except Exception as exc:  # noqa: BLE001 - fail-soft neutral
        _LOG.debug("context_switching read failed: %s", exc)
        return MetricRow(name, 0.0, "stddev_org_debt", source, f"org_debt unavailable: {exc}", sources_missing=True)


def _cognitive_overhead(
    saturation_reader: Optional[Callable[[], dict[str, float]]],
) -> MetricRow:
    """Mean saturation risk across verticals as a cognitive-overhead proxy.

    Over-explored verticals yield diminishing returns yet still consume
    reasoning / attention cycles. The population-mean saturation risk over
    the verticals reported by ``saturation_monitor`` is a compact overhead
    signal - saturation_monitor is a pure function so the caller supplies
    the trials-by-vertical dict; a missing supplier degrades to neutral.
    """
    name = "cognitive_overhead"
    source = "backend.strategy.saturation_monitor.saturation_risk_by_vertical"
    try:
        reader = saturation_reader
        if reader is None:
            return MetricRow(
                name, 0.0, "mean_saturation_risk", source,
                "no trials-by-vertical supplier configured", sources_missing=True,
            )
        risks = reader() or {}
        if not risks:
            return MetricRow(name, 0.0, "mean_saturation_risk", source, "no verticals reported")
        values = [float(v) for v in risks.values()]
        mean = sum(values) / len(values)
        peak = max(values) if values else 0.0
        detail = f"{len(values)} verticals, mean risk {mean:.3f}, peak {peak:.3f}"
        return MetricRow(name, round(mean, 4), "mean_saturation_risk", source, detail)
    except Exception as exc:  # noqa: BLE001 - fail-soft neutral
        _LOG.debug("cognitive_overhead read failed: %s", exc)
        return MetricRow(name, 0.0, "mean_saturation_risk", source, f"saturation unavailable: {exc}", sources_missing=True)


def _approval_friction(
    approvals_reader_all: Optional[Callable[[], dict[str, list[dict[str, Any]]]]],
) -> MetricRow:
    """Share of recent approvals that expired unresolved.

    ``expired / (pending + approved + rejected + expired)`` over the visible
    queue. High = friction - approvals are being filed but the operator is
    not clearing them before TTL, which fail-closes downstream work. Uses
    the same fail-closed expiry that ``approvals.list_approvals`` already
    applies on read, so a stale pending row is counted as expired.
    """
    name = "approval_friction"
    source = "backend.common.approvals.list_approvals(status=None)"
    try:
        reader = approvals_reader_all
        if reader is None:
            from backend.common.approvals import (
                STATUS_APPROVED,
                STATUS_EXPIRED,
                STATUS_PENDING,
                STATUS_REJECTED,
                list_approvals,
            )

            def reader() -> dict[str, list[dict[str, Any]]]:
                return {
                    STATUS_PENDING: list_approvals(status=STATUS_PENDING, limit=200),
                    STATUS_APPROVED: list_approvals(status=STATUS_APPROVED, limit=200),
                    STATUS_REJECTED: list_approvals(status=STATUS_REJECTED, limit=200),
                    STATUS_EXPIRED: list_approvals(status=STATUS_EXPIRED, limit=200),
                }
        buckets = reader() or {}
        counts = {status: len(rows or []) for status, rows in buckets.items()}
        total = sum(counts.values())
        if total <= 0:
            return MetricRow(name, 0.0, "expired_share", source, "no approvals visible")
        expired = float(counts.get("expired", 0))
        friction = expired / float(total)
        detail = (
            f"{int(expired)}/{total} expired ({friction * 100.0:.1f}%); "
            f"pending={counts.get('pending', 0)}, approved={counts.get('approved', 0)}, "
            f"rejected={counts.get('rejected', 0)}"
        )
        return MetricRow(name, round(friction, 4), "expired_share", source, detail)
    except Exception as exc:  # noqa: BLE001 - fail-soft neutral
        _LOG.debug("approval_friction read failed: %s", exc)
        return MetricRow(name, 0.0, "expired_share", source, f"approvals unavailable: {exc}", sources_missing=True)


def _communication_entropy(
    regret_reader: Optional[Callable[[], tuple[float, float]]],
) -> MetricRow:
    """Regret-per-token as a communication-entropy proxy.

    High cumulative bandit regret for the tokens spent means signal is being
    wasted in the exploration/exploitation trade-off - the equivalent of
    entropy in the agent's downstream messaging. Uses regret_engine's own
    ``regret_per_token`` so the arithmetic (including the token epsilon)
    matches the strategy layer exactly.
    """
    name = "communication_entropy"
    source = "backend.strategy.regret_engine.regret_per_token"
    try:
        reader = regret_reader
        if reader is None:
            return MetricRow(
                name, 0.0, "regret_per_token", source,
                "no regret-supplier configured", sources_missing=True,
            )
        cumulative, token_spend = reader()
        cumulative = float(cumulative)
        token_spend = float(token_spend) if token_spend else _REGRET_TOKEN_BASELINE
        from backend.strategy.regret_engine import regret_per_token
        rpt = regret_per_token(cumulative, token_spend)
        detail = f"cumulative regret {cumulative:.4f} over token_spend {token_spend:.2f}"
        return MetricRow(name, round(rpt, 6), "regret_per_token", source, detail)
    except Exception as exc:  # noqa: BLE001 - fail-soft neutral
        _LOG.debug("communication_entropy read failed: %s", exc)
        return MetricRow(name, 0.0, "regret_per_token", source, f"regret unavailable: {exc}", sources_missing=True)


# ---------------------------------------------------------------------------
# ISO helper - kept local to avoid coupling to approvals internals
# ---------------------------------------------------------------------------

def _iso_to_epoch(value: str) -> float:
    import calendar

    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def compute_organizational_economics(
    *,
    now_ts: Optional[float] = None,
    org_debt_reader: Optional[Callable[[], dict[str, Any]]] = None,
    approvals_pending_reader: Optional[Callable[[], list[dict[str, Any]]]] = None,
    approvals_all_reader: Optional[Callable[[], dict[str, list[dict[str, Any]]]]] = None,
    saturation_reader: Optional[Callable[[], dict[str, float]]] = None,
    regret_reader: Optional[Callable[[], tuple[float, float]]] = None,
) -> OrganizationalEconomicsReport:
    """Build the six-metric organizational-economics report.

    Every source reader is an injectable seam so the report is deterministic
    under test and never touches live ledgers when a caller supplies mocks.
    Never raises; kill-switch => empty enabled=False report.
    """
    now = now_ts if now_ts is not None else time.time()
    report = OrganizationalEconomicsReport(generated_ts=now, enabled=_enabled())
    if not report.enabled:
        return report

    report.metrics.append(_coordination_cost(org_debt_reader))
    report.metrics.append(_decision_latency(approvals_pending_reader, now))
    report.metrics.append(_context_switching(org_debt_reader))
    report.metrics.append(_cognitive_overhead(saturation_reader))
    report.metrics.append(_approval_friction(approvals_all_reader))
    report.metrics.append(_communication_entropy(regret_reader))
    return report


# ---------------------------------------------------------------------------
# CLI - ad-hoc run, mirrors production_health.main() shape
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:  # noqa: ARG001 - argv reserved
    logging.basicConfig(
        level=os.getenv("SAMUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    report = compute_organizational_economics()
    if not report.enabled:
        print("organizational economics: disabled (SAMUS_ORG_ECONOMICS_REPORT_ENABLED)")
        return 0
    print(report.summary_line())
    for m in report.metrics:
        tag = " [degraded]" if m.sources_missing else ""
        print(f"  {m.name}{tag} = {m.value} {m.unit}")
        print(f"      source: {m.source}")
        print(f"      detail: {m.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
