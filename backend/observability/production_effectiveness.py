"""Conversion-effectiveness checks — the complement to production_health.

``production_health`` watches LIVENESS + compliance: is the machine running,
are scheduled tasks firing, is inbound opt-out drained (CAN-SPAM). Those checks
can all be green while the business produces $0 — enrichment churns, nothing
stakes, opportunities sit at $0 deal size, calls flatline. A liveness-only
monitor reports "healthy" straight through a starved funnel (the exact trap the
2026-07-06 production review fell into before checking the real numbers).

This module watches EFFECTIVENESS: is the machine actually CONVERTING? Each
check maps to a failure mode found in that review:

  * staking starvation  — auto-stake scans candidates but stakes zero
  * scoring gap         — most prospects never get a lead_score
  * zero-value pipeline — opportunities created with deal_size_usd == 0
  * call pace           — calls placed far below the daily goal in-hours
  * contactability floor— share of prospects with a usable email
  * funnel leakage      — opportunities present but none reach proposal

Design:
  * READ-ONLY. Never mutates business state.
  * FAIL-OPEN. A data fault yields UNKNOWN for that check, never raises — a
    monitor that crashes on its own telemetry is worse than a blind spot.
  * Data access is behind :class:`EffectivenessProvider` so the checks are
    pure and unit-testable offline; the default provider reads DynamoDB +
    the control-tick ledger + CRM stats.
  * :class:`EffStatus` mirrors ``production_health.HealthStatus`` value-for-
    value so this report folds into that monitor through a thin adapter once
    it lands on origin (see ``to_health_checks``).

Thresholds are module constants, each overridable by an env var so the
operator can tune sensitivity without a redeploy.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

_LOG = logging.getLogger("samus.observability.production_effectiveness")


class EffStatus(str, Enum):
    """Severity, mirroring production_health.HealthStatus value-for-value."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


# --- tunable thresholds (env-overridable) ----------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# Share of prospects with NO lead_score above which scoring is failing/warning.
UNSCORED_FAIL_FRAC = _env_float("SAMUS_EFF_UNSCORED_FAIL_FRAC", 0.60)
UNSCORED_WARN_FRAC = _env_float("SAMUS_EFF_UNSCORED_WARN_FRAC", 0.30)
# Share of prospects WITH a usable email below which contactability fails/warns.
CONTACT_FAIL_FRAC = _env_float("SAMUS_EFF_CONTACT_FAIL_FRAC", 0.10)
CONTACT_WARN_FRAC = _env_float("SAMUS_EFF_CONTACT_WARN_FRAC", 0.30)
# Fraction of the daily call goal that must be met (scaled by day-elapsed)
# before the call-pace check trips.
CALL_PACE_FAIL_FRAC = _env_float("SAMUS_EFF_CALL_PACE_FAIL_FRAC", 0.25)
CALL_PACE_WARN_FRAC = _env_float("SAMUS_EFF_CALL_PACE_WARN_FRAC", 0.60)


@dataclass(frozen=True)
class EffCheck:
    """One effectiveness check outcome."""

    name: str
    status: EffStatus
    detail: str
    metric: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "metric": self.metric,
        }


@dataclass
class EffReport:
    """Aggregate effectiveness report."""

    checks: list[EffCheck] = field(default_factory=list)
    generated_at: str = ""

    @property
    def alerting(self) -> bool:
        """True if any check is FAIL/CRITICAL (the alert-worthy set)."""
        return any(
            c.status in (EffStatus.FAIL, EffStatus.CRITICAL) for c in self.checks
        )

    @property
    def worst(self) -> EffStatus:
        order = [
            EffStatus.CRITICAL, EffStatus.FAIL, EffStatus.WARN,
            EffStatus.UNKNOWN, EffStatus.INFO, EffStatus.OK,
        ]
        for s in order:
            if any(c.status is s for c in self.checks):
                return s
        return EffStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "alerting": self.alerting,
            "worst": self.worst.value,
            "checks": [c.to_dict() for c in self.checks],
        }


class EffectivenessProvider(Protocol):
    """Data access seam. Each method returns raw numbers or None on fault.

    Implementations MUST NOT raise — return None so the check degrades to
    UNKNOWN rather than taking the monitor down.
    """

    def prospect_counts(self) -> Optional[tuple[int, int, int]]:
        """``(total, scored, with_email)`` across the prospect store."""

    def opportunity_counts(self) -> Optional[tuple[int, int]]:
        """``(open_opps, zero_value_open_opps)``."""

    def last_auto_stake(self) -> Optional[tuple[int, int]]:
        """Most recent sweep ``(scanned, staked)``; None if no sweep seen."""

    def call_pace(self) -> Optional[tuple[int, int, float]]:
        """``(calls_today, calls_goal, day_fraction_elapsed)``."""

    def funnel_counts(self) -> Optional[tuple[int, int]]:
        """``(opportunity_stage_count, proposal_stage_count)`` today."""


# --- individual checks (pure) ----------------------------------------------

def _check_scoring(provider: EffectivenessProvider) -> EffCheck:
    name = "scoring_coverage"
    counts = _safe(provider.prospect_counts)
    if counts is None:
        return EffCheck(name, EffStatus.UNKNOWN, "prospect counts unavailable")
    total, scored, _ = counts
    if total <= 0:
        return EffCheck(name, EffStatus.INFO, "no prospects in store")
    unscored_frac = (total - scored) / total
    detail = f"{total - scored}/{total} prospects unscored ({unscored_frac:.0%})"
    if unscored_frac >= UNSCORED_FAIL_FRAC:
        return EffCheck(name, EffStatus.FAIL, detail, unscored_frac)
    if unscored_frac >= UNSCORED_WARN_FRAC:
        return EffCheck(name, EffStatus.WARN, detail, unscored_frac)
    return EffCheck(name, EffStatus.OK, detail, unscored_frac)


def _check_contactability(provider: EffectivenessProvider) -> EffCheck:
    name = "contactability"
    counts = _safe(provider.prospect_counts)
    if counts is None:
        return EffCheck(name, EffStatus.UNKNOWN, "prospect counts unavailable")
    total, _, with_email = counts
    if total <= 0:
        return EffCheck(name, EffStatus.INFO, "no prospects in store")
    frac = with_email / total
    detail = f"{with_email}/{total} prospects have an email ({frac:.0%})"
    if frac < CONTACT_FAIL_FRAC:
        return EffCheck(name, EffStatus.FAIL, detail, frac)
    if frac < CONTACT_WARN_FRAC:
        return EffCheck(name, EffStatus.WARN, detail, frac)
    return EffCheck(name, EffStatus.OK, detail, frac)


def _check_staking(provider: EffectivenessProvider) -> EffCheck:
    name = "staking_throughput"
    sweep = _safe(provider.last_auto_stake)
    if sweep is None:
        return EffCheck(name, EffStatus.UNKNOWN, "no auto-stake sweep recorded")
    scanned, staked = sweep
    detail = f"last sweep scanned={scanned} staked={staked}"
    if scanned > 0 and staked == 0:
        return EffCheck(
            name, EffStatus.FAIL,
            detail + " — candidates found but none qualified to stake", 0.0,
        )
    return EffCheck(name, EffStatus.OK, detail, float(staked))


def _check_opportunity_value(provider: EffectivenessProvider) -> EffCheck:
    name = "opportunity_value"
    counts = _safe(provider.opportunity_counts)
    if counts is None:
        return EffCheck(name, EffStatus.UNKNOWN, "opportunity counts unavailable")
    open_opps, zero_value = counts
    if open_opps <= 0:
        return EffCheck(name, EffStatus.INFO, "no open opportunities")
    frac = zero_value / open_opps
    detail = f"{zero_value}/{open_opps} open opportunities have $0 deal size"
    if zero_value == open_opps:
        return EffCheck(name, EffStatus.FAIL, detail + " — pipeline unvalued", frac)
    if frac >= 0.5:
        return EffCheck(name, EffStatus.WARN, detail, frac)
    return EffCheck(name, EffStatus.OK, detail, frac)


def _check_call_pace(provider: EffectivenessProvider) -> EffCheck:
    name = "call_pace"
    pace = _safe(provider.call_pace)
    if pace is None:
        return EffCheck(name, EffStatus.UNKNOWN, "call stats unavailable")
    calls_today, goal, day_frac = pace
    if goal <= 0:
        return EffCheck(name, EffStatus.INFO, "no call goal set")
    day_frac = min(max(day_frac, 0.0), 1.0)
    expected = goal * day_frac
    attainment = (calls_today / expected) if expected > 0 else 1.0
    detail = (
        f"calls {calls_today}/{goal} goal; "
        f"{attainment:.0%} of pace-adjusted target ({expected:.0f})"
    )
    if expected <= 0:
        return EffCheck(name, EffStatus.OK, detail, attainment)
    if attainment < CALL_PACE_FAIL_FRAC:
        return EffCheck(name, EffStatus.FAIL, detail, attainment)
    if attainment < CALL_PACE_WARN_FRAC:
        return EffCheck(name, EffStatus.WARN, detail, attainment)
    return EffCheck(name, EffStatus.OK, detail, attainment)


def _check_funnel_leakage(provider: EffectivenessProvider) -> EffCheck:
    name = "funnel_progression"
    counts = _safe(provider.funnel_counts)
    if counts is None:
        return EffCheck(name, EffStatus.UNKNOWN, "funnel counts unavailable")
    opps, proposals = counts
    if opps <= 0:
        return EffCheck(name, EffStatus.INFO, "no opportunities to progress")
    detail = f"{opps} opportunities, {proposals} reached proposal"
    if proposals == 0:
        return EffCheck(
            name, EffStatus.WARN,
            detail + " — opportunities not advancing to proposal", 0.0,
        )
    return EffCheck(name, EffStatus.OK, detail, float(proposals))


_CHECKS = (
    _check_scoring,
    _check_contactability,
    _check_staking,
    _check_opportunity_value,
    _check_call_pace,
    _check_funnel_leakage,
)


def check_production_effectiveness(
    provider: Optional[EffectivenessProvider] = None,
    *,
    now: Optional[_dt.datetime] = None,
) -> EffReport:
    """Run every effectiveness check and return an aggregate report.

    ``provider`` defaults to the live DynamoDB-backed provider; tests inject a
    fake. Never raises — a provider fault surfaces as UNKNOWN per check.
    """
    if provider is None:
        provider = DynamoEffectivenessProvider()
    stamp = (now or _dt.datetime.now(_dt.timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    checks = [fn(provider) for fn in _CHECKS]
    return EffReport(checks=checks, generated_at=stamp)


def _safe(fn: Any) -> Any:
    """Call a provider method, swallowing faults into None (fail-open)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        _LOG.warning("effectiveness provider %s faulted: %s", getattr(fn, "__name__", fn), exc)
        return None


# --- default live provider -------------------------------------------------

class DynamoEffectivenessProvider:
    """Live provider: DynamoDB scans + control-tick ledger + CRM stats.

    Bounded scans (``Select='COUNT'`` where possible) keep this cheap enough
    to run on the monitor cadence. Every method returns None on fault so the
    checks degrade to UNKNOWN.
    """

    def __init__(self, region: str = "") -> None:
        self._region = region or os.environ.get("AWS_REGION", "us-west-1")

    def _table(self, name: str) -> Any:
        import boto3

        return boto3.resource("dynamodb", region_name=self._region).Table(name)

    def prospect_counts(self) -> Optional[tuple[int, int, int]]:
        try:
            t = self._table("samus_prospects")
            total = scored = with_email = 0
            kwargs: dict[str, Any] = {
                "ProjectionExpression": "lead_score, owner_email"
            }
            while True:
                resp = t.scan(**kwargs)
                for it in resp.get("Items", []):
                    total += 1
                    if str(it.get("lead_score") or "").strip():
                        scored += 1
                    if str(it.get("owner_email") or "").strip():
                        with_email += 1
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
            return total, scored, with_email
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("prospect_counts failed: %s", exc)
            return None

    def opportunity_counts(self) -> Optional[tuple[int, int]]:
        try:
            t = self._table("samus_opportunities")
            open_opps = zero_value = 0
            kwargs: dict[str, Any] = {}
            while True:
                resp = t.scan(**kwargs)
                for it in resp.get("Items", []):
                    stage = str(it.get("stage") or it.get("samus_state") or "")
                    if stage in ("closed_won", "closed_lost"):
                        continue
                    open_opps += 1
                    if float(it.get("deal_size_usd") or 0) == 0:
                        zero_value += 1
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
            return open_opps, zero_value
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("opportunity_counts failed: %s", exc)
            return None

    def last_auto_stake(self) -> Optional[tuple[int, int]]:
        try:
            from backend.common import control_tick_ledger

            recent = control_tick_ledger.recent_ticks(limit=25)
            for tick in reversed(recent.get("ticks", [])):
                stake = tick.get("auto_stake") or {}
                if "scanned" in stake:
                    return int(stake.get("scanned") or 0), int(stake.get("staked") or 0)
            return None
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("last_auto_stake failed: %s", exc)
            return None

    def call_pace(self) -> Optional[tuple[int, int, float]]:
        try:
            from backend.crm import service as crm_service

            stats = crm_service.daily_stats()
            calls = int(stats.get("calls_today") or 0)
            goal = int(stats.get("calls_goal") or 0)
            now = _dt.datetime.now(_dt.timezone.utc)
            # Business day proxy: 13:00–01:00 UTC ~ 6am–6pm PT. Fraction of the
            # 12h window elapsed, clamped to [0,1].
            elapsed_h = (now.hour + now.minute / 60.0) - 13.0
            day_frac = min(max(elapsed_h / 12.0, 0.0), 1.0)
            return calls, goal, day_frac
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("call_pace failed: %s", exc)
            return None

    def funnel_counts(self) -> Optional[tuple[int, int]]:
        try:
            t = self._table("samus_opportunities")
            opp = prop = 0
            kwargs: dict[str, Any] = {"ProjectionExpression": "stage, samus_state"}
            while True:
                resp = t.scan(**kwargs)
                for it in resp.get("Items", []):
                    stage = str(it.get("stage") or it.get("samus_state") or "")
                    if stage in ("opportunity", "new", "staked", "qualified"):
                        opp += 1
                    elif stage == "proposal":
                        prop += 1
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
            return opp, prop
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("funnel_counts failed: %s", exc)
            return None


def to_health_checks(report: EffReport) -> list[dict[str, Any]]:
    """Adapter: flatten an EffReport to the (name, status) dict shape the
    liveness ``production_health`` monitor emits, so the two can be merged
    into one operator report once that module lands on origin.
    """
    return [{"name": c.name, "status": c.status.value, "detail": c.detail} for c in report.checks]


__all__ = [
    "EffStatus",
    "EffCheck",
    "EffReport",
    "EffectivenessProvider",
    "DynamoEffectivenessProvider",
    "check_production_effectiveness",
    "to_health_checks",
]
