"""Stalled-opportunity revival - the missing "advance existing opps" producer.

Motivation (2026-07-07 diagnosis, framework-agent audit):
  On 2026-07-07 pre-production sweep, the funnel had 31 opportunities frozen
  at ``stage=new`` for up to 48 days (Kelly Zimmerman's opportunity had been
  parked since 2026-05-20). Auto-stake correctly skipped them (rule: don't
  re-stake a prospect that already has an opportunity). But NOTHING else
  advanced them either: no producer re-reviewed existing new-stage opps, so
  they sat inert while auto-stake kept scanning newly-qualified prospects and
  finding they too "already had an opportunity". Net effect: the cash engine
  starved for weeks while the operator watched the auto-stake sweep report
  ``scanned=N staked=0 skipped=N`` every 30 min.

  This module closes the gap. Same shape as ``auto_stake.run_auto_stake_sweep``
  (called from the control-tick loop next to auto-stake), but instead of
  CREATING new opportunities, it FINDS existing stage=new opportunities that
  are older than the freshness threshold and re-issues a
  :class:`RevenueTriggerRequest` with ``trigger_source='reengagement'``. The
  Codex Gate does the actual filtering (stake sentence present, action clean);
  a blocked opp gets escalated with the failing protocol named, exactly like
  a fresh auto-stake trigger would.

Kill switches + tuning knobs (all fail-open on invalid values):
  * ``SAMUS_STALLED_REVIVAL_ENABLED``       - master (default ON)
  * ``SAMUS_STALLED_REVIVAL_MIN_AGE_HOURS`` - only revive opps older than this
    (default 12h - so today's fresh auto-stakes get a normal cash-engine walk
    first, and only genuinely-parked opps enter the revival lane)
  * ``SAMUS_STALLED_REVIVAL_MAX_PER_SWEEP`` - cap so a large parked backlog
    doesn't burst-send (default 5; heat throttle further reduces)
  * ``SAMUS_STALLED_REVIVAL_SCAN_LIMIT``    - DDB scan cap (default 200)

Fail-safe: every DDB / codex / karma fault degrades the sweep to a partial
result rather than raising. The control tick treats a partial as success and
tries again next tick.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger("samus.cash_engine.stalled_revival")

ENV_ENABLED = "SAMUS_STALLED_REVIVAL_ENABLED"
ENV_MIN_AGE_HOURS = "SAMUS_STALLED_REVIVAL_MIN_AGE_HOURS"
ENV_MAX_PER_SWEEP = "SAMUS_STALLED_REVIVAL_MAX_PER_SWEEP"
ENV_SCAN_LIMIT = "SAMUS_STALLED_REVIVAL_SCAN_LIMIT"

_DEFAULT_MIN_AGE_HOURS = 12
_DEFAULT_MAX_PER_SWEEP = 5
_DEFAULT_SCAN_LIMIT = 200


def _flag_on(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _parse_created_at(raw: Any) -> datetime | None:
    """Best-effort ISO-8601 parse. Returns None on anything malformed so the
    caller can skip the row rather than crash the sweep on a bad record."""
    if not raw:
        return None
    s = str(raw)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def _stalled_opps(scan_limit: int, min_age_hours: int) -> list[dict[str, Any]]:
    """DDB scan for opportunities stuck at stage=new past the age threshold.

    Sorted oldest-first so the most-stuck deals get the earliest revival
    attempt. Empty on any DDB fault (fail-open at the outer edge).
    """
    try:
        import boto3
    except Exception:  # noqa: BLE001
        _LOG.exception("boto3 missing; stalled_revival skipped")
        return []

    ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    table = ddb.Table(os.environ.get("DDB_OPPORTUNITIES_TABLE", "samus_opportunities"))
    try:
        # Small ledger, full scan is fine. If this ever needs to run over
        # 10k opportunities, swap in a filter expression on stage.
        rows = table.scan(Limit=scan_limit).get("Items", [])
    except Exception:  # noqa: BLE001
        _LOG.exception("stalled_revival DDB scan failed")
        return []

    now = datetime.now(timezone.utc)
    cutoff_seconds = min_age_hours * 3600.0
    stalled: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("stage") or "").lower() != "new":
            continue
        created = _parse_created_at(row.get("created_at"))
        if created is None:
            continue
        age = (now - created).total_seconds()
        if age < cutoff_seconds:
            continue
        stalled.append(row)

    stalled.sort(key=lambda r: str(r.get("created_at") or ""))
    return stalled


def _revive_one(opp: dict[str, Any]) -> dict[str, Any]:
    """One review_opportunity call with trigger_source='reengagement'. Never
    raises. Returns a per-opp summary the sweep tally consumes."""
    pid = str(opp.get("prospect_id") or "").strip()
    if not pid:
        return {"ok": False, "reason": "missing_prospect_id"}
    try:
        from backend.cash_engine.models import RevenueTriggerRequest
        from backend.cash_engine.service import review_opportunity
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"import_failed:{exc}"}

    try:
        req = RevenueTriggerRequest(
            prospect_id=pid,
            trigger_source="reengagement",
            trigger_reason="stalled_new_stage_revival",
            current_samus_state="staked",
        )
        r = review_opportunity(req)
        return {
            "ok": bool(getattr(r, "accepted", False)),
            "prospect_id": pid,
            "opportunity_id": str(opp.get("opportunity_id") or ""),
            "status": str(getattr(r, "status", "")),
            "reason": str(getattr(r, "reason", "") or "")[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "prospect_id": pid, "reason": f"review_failed:{exc}"}


def run_stalled_revival_sweep() -> dict[str, Any]:
    """One sweep: find stalled opps, revive up to max_per_sweep of them.

    Returns a tally the control-tick can log + surface in the morning brief.
    Never raises; degrades to enabled=False / errors captured in the tally.
    """
    if not _flag_on(ENV_ENABLED):
        return {"enabled": False, "scanned": 0, "revived": 0, "skipped": 0}

    min_age_hours = _env_int(ENV_MIN_AGE_HOURS, _DEFAULT_MIN_AGE_HOURS)
    max_per_sweep = _env_int(ENV_MAX_PER_SWEEP, _DEFAULT_MAX_PER_SWEEP)
    scan_limit = _env_int(ENV_SCAN_LIMIT, _DEFAULT_SCAN_LIMIT)

    # Same heat throttle auto-stake uses (delegating to backend.heat.service.
    # send_multiplier_now so a hot fatigued fleet reduces revival volume in
    # lockstep with fresh auto-stake).
    try:
        from backend.heat import service as _heat

        mult = _heat.send_multiplier_now()
        if mult < 1.0:
            scaled = int(max_per_sweep * mult)
            _LOG.info(
                "stalled_revival heat throttle: mult=%.2f max_per_sweep %d->%d",
                mult,
                max_per_sweep,
                scaled,
            )
            max_per_sweep = scaled
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("stalled_revival heat multiplier failed: %s", exc)

    opps = _stalled_opps(scan_limit, min_age_hours)
    scanned = len(opps)
    _LOG.info(
        "stalled_revival sweep: scanned=%d min_age_hours=%d max_per_sweep=%d",
        scanned,
        min_age_hours,
        max_per_sweep,
    )

    revived = 0
    skipped = 0
    results: list[dict[str, Any]] = []
    for opp in opps:
        if revived >= max_per_sweep:
            skipped += 1
            continue
        r = _revive_one(opp)
        results.append(r)
        if r.get("ok"):
            revived += 1
        else:
            # Codex-blocked / no-stake-sentence / failed - counts as skipped
            # for the top-line tally (the reason is on the per-opp record).
            skipped += 1

    tally = {
        "enabled": True,
        "scanned": scanned,
        "revived": revived,
        "skipped": skipped,
        "min_age_hours": min_age_hours,
        "max_per_sweep": max_per_sweep,
        "results": results,
    }
    _LOG.info(
        "stalled_revival sweep complete: scanned=%d revived=%d skipped=%d",
        scanned,
        revived,
        skipped,
    )
    return tally


__all__ = [
    "run_stalled_revival_sweep",
    "ENV_ENABLED",
    "ENV_MIN_AGE_HOURS",
    "ENV_MAX_PER_SWEEP",
    "ENV_SCAN_LIMIT",
]
