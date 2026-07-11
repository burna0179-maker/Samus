"""Stack-level control-loop tick — observe -> decide, in one pass.

WHAT THIS IS
------------
The two autonomous workcells ``entropy`` and ``portfolio_controller`` are
fully built but had no producer: nothing in the stack ran the observe->decide
loop they were designed to be driven by. This module is that loop.

``run_control_tick`` performs ONE pass:

  1. OBSERVE — call :func:`backend.entropy.service.scan`. The entropy workcell
     computes a weighted systemic-instability score from queue / error /
     retry / LLM-failure signals and recommends countermeasures.
  2. DECIDE  — call :func:`backend.portfolio_controller.service.run_rebalance`.
     The portfolio controller rebalances token-quota / priority-weight levers
     across tracked workcells from per-workcell efficiency signals.
  3. RECORD  — append the combined snapshot to the control-tick JSONL ledger
     (:mod:`backend.common.control_tick_ledger`) so the loop is observable.
  4. RETURN  — a combined snapshot dict the operator/scheduler sees.

WHY IN-PROCESS
--------------
Both workcells are zero-LLM, fully deterministic, and import cleanly into the
gateway process. Their service functions (``scan`` / ``run_rebalance``) are
the published business entry points. Calling them in-process avoids a
self-dispatch HTTP round-trip and keeps the tick a single synchronous pass —
the same shape ``intake``'s ``/intake/poll-inbox`` uses for its drain pass.

BEST-EFFORT
-----------
Each sub-call is wrapped: a failure in one stage is captured as a structured
``error`` field on that stage and the tick continues. One sub-call failing
never sinks the whole tick — the operator still gets the partial snapshot and
a recorded ledger row.

IDEMPOTENT / SAFE ON ANY CADENCE
--------------------------------
The tick appends one observability row AND (when enforcement is armed) applies
the rebalance's token-quota recommendations to the LLM-budget store via
:meth:`LlmBudgetStore.set_quota_override` — a TTL-bounded, clamped
([0.25x, 2.0x] of base), auto-expiring override that never touches other
workcells' code. Each override is refreshed every tick and lapses within its
TTL if the loop stops, so running the tick twice, or on any schedule, stays
safe and self-healing. It mirrors ``/intake/poll-inbox``: a manual/scheduled
trigger returning per-pass counters.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from backend.common import control_tick_ledger
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.gateway.control_tick")

# Enforcement of the rebalance's per-workcell quota recommendations is now
# WIRED: the ENFORCE stage applies each quota_cut to the LLM-budget store via
# the TTL-bounded set_quota_override lever (backend/common/llm_budget.py). The
# override is clamped + auto-expiring, so enforcement stays reversible and
# confined to the in-scope common LLM-budget surface — it does NOT edit other
# workcells' code. Armed by default; kill-switch SAMUS_CONTROL_TICK_ENFORCE=0.
ENFORCEMENT_FOLLOWUP: str = (
    "control-tick applies portfolio quota-cut recommendations to the "
    "LLM-budget store via TTL-bounded set_quota_override (clamped, "
    "auto-expiring); disable with SAMUS_CONTROL_TICK_ENFORCE=0"
)

# Enforcement kill-switch + override TTL (env-configured to avoid coupling the
# gateway tick to the shared settings model). Default ARMED. The TTL is how
# long an applied override survives without a refreshing tick; the store clamps
# it to <=24h. Each tick refreshes the override, so a live loop keeps it fresh
# while a stopped loop lets it lapse (fail-safe back to the adaptive quota).
_ENFORCE_ENV: str = "SAMUS_CONTROL_TICK_ENFORCE"
_ENFORCE_TTL_ENV: str = "SAMUS_CONTROL_TICK_ENFORCE_TTL_SEC"
_ENFORCE_TTL_DEFAULT: float = 3600.0


def _enforcement_enabled() -> bool:
    """True unless SAMUS_CONTROL_TICK_ENFORCE is an explicit falsey value."""
    return os.getenv(_ENFORCE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _enforcement_ttl() -> float:
    """Override TTL in seconds (env-overridable). Bad values -> default."""
    try:
        ttl = float(os.getenv(_ENFORCE_TTL_ENV, "") or _ENFORCE_TTL_DEFAULT)
        return ttl if ttl > 0 else _ENFORCE_TTL_DEFAULT
    except (TypeError, ValueError):
        return _ENFORCE_TTL_DEFAULT


# Default entropy-scan inputs when the caller supplies none. All-zero is the
# benign baseline the EntropyScanRequest model itself defaults to — a tick
# with no observed signals reports a stable, all-clear score rather than
# fabricating instability.
_DEFAULT_ENTROPY_INPUTS: dict[str, Any] = {}


def _run_entropy_scan(task_id: str, entropy_inputs: dict[str, Any]) -> dict[str, Any]:
    """Best-effort OBSERVE stage — call entropy.scan, capture failure.

    Returns a stage dict that always has an ``ok`` bool. On success it carries
    the EntropyScanResponse fields; on failure it carries an ``error`` string
    and zeroed score so the combined snapshot stays well-formed.
    """
    try:
        # Deferred imports: keep the gateway import graph light and avoid a
        # hard import-time coupling to the workcell modules.
        from backend.entropy.models import EntropyScanRequest
        from backend.entropy.service import scan as entropy_scan

        req = EntropyScanRequest.model_validate(entropy_inputs or {})
        result = entropy_scan(task_id, req)
        return {
            "ok": True,
            "entropy_score": result.entropy_score,
            "stable": result.stable,
            "countermeasures": list(result.countermeasures),
            "persisted": result.persisted,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - best-effort: never sink the tick
        _LOG.warning("control_tick entropy scan failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "entropy_score": None,
            "stable": None,
            "countermeasures": [],
            "persisted": False,
            "error": f"entropy_scan_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_rebalance(task_id: str, workcells: list[dict[str, Any]]) -> dict[str, Any]:
    """Best-effort DECIDE stage — call portfolio_controller.run_rebalance.

    ``workcells`` is an optional list of WorkcellInput-shaped dicts; when
    empty the portfolio controller rebalances its DEFAULT_TRACKED_WORKCELLS.
    Returns a stage dict that always has an ``ok`` bool.
    """
    try:
        from backend.portfolio_controller.models import RebalanceRequest
        from backend.portfolio_controller.service import run_rebalance

        req = RebalanceRequest.model_validate({"workcells": workcells or [], "task_id": task_id})
        result = run_rebalance(req)
        # Project the per-workcell quota / priority recommendations the
        # operator needs to see. Only the workcells where a rule actually
        # fired are interesting for the snapshot; the full set is on the
        # ledger row for completeness.
        adjustments = [
            {
                "workcell": a.workcell,
                "token_quota": a.token_quota,
                "priority_weight": a.priority_weight,
                "quota_cut": a.quota_cut,
                "priority_boosted": a.priority_boosted,
            }
            for a in result.allocations
        ]
        return {
            "ok": True,
            "workcell_count": result.workcell_count,
            "quota_cuts": result.quota_cuts,
            "priority_boosts": result.priority_boosts,
            "adjustments": adjustments,
            "persisted": result.persisted,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - best-effort: never sink the tick
        _LOG.warning("control_tick rebalance failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "workcell_count": 0,
            "quota_cuts": 0,
            "priority_boosts": 0,
            "adjustments": [],
            "persisted": False,
            "error": f"rebalance_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_enforcement(task_id: str, portfolio_stage: dict[str, Any]) -> dict[str, Any]:
    """ENFORCE stage — apply the rebalance's quota cuts to the LLM-budget store.

    For every workcell where the portfolio rebalance fired a ``quota_cut``, set
    a TTL-bounded quota override via
    :meth:`backend.common.llm_budget.LlmBudgetStore.set_quota_override`. The
    store clamps the value to [0.25x, 2.0x] of base and the TTL to <=24h, so a
    bad recommendation can neither zero a workcell out nor pin it forever.

    Only quota *cuts* are enforced — the protective direction. Priority-weight
    boosts have no LLM-budget lever (priority is a scheduling concern owned by
    other workcells) and stay advisory on the snapshot. Best-effort: a store
    failure is captured as ``error`` and never sinks the tick. A no-op (returns
    ``enabled=False``) when SAMUS_CONTROL_TICK_ENFORCE is disabled.
    """
    if not _enforcement_enabled():
        return {"ok": True, "enabled": False, "applied": 0, "overrides": [], "error": None}

    overrides: list[dict[str, Any]] = []
    try:
        from backend.common.llm_budget import get_store

        store = get_store()
        ttl = _enforcement_ttl()
        for adj in portfolio_stage.get("adjustments", []):
            if not adj.get("quota_cut"):
                continue
            workcell = adj["workcell"]
            b = store.set_quota_override(workcell, int(adj["token_quota"]), ttl)
            overrides.append(
                {
                    "workcell": workcell,
                    "quota": b.quota_override,
                    "expires_at": b.quota_override_expires_at,
                }
            )
        return {
            "ok": True,
            "enabled": True,
            "applied": len(overrides),
            "overrides": overrides,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — best-effort: never sink the tick
        _LOG.warning("control_tick enforcement failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "enabled": True,
            "applied": len(overrides),
            "overrides": overrides,
            "error": f"enforcement_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_auto_stake(task_id: str) -> dict[str, Any]:
    """ACT stage — promote qualified prospects to staked opportunities.

    See backend/cash_engine/auto_stake.py. Fail-soft: any exception returns
    a stage dict with ok=False + error; never re-raised. When the arming
    flag is OFF the sweep is a no-op that returns ``{"enabled": False}``,
    which we surface so the operator can see at a glance whether the
    autonomous expansion loop is armed.
    """
    try:
        from backend.cash_engine.auto_stake import run_auto_stake_sweep

        summary = run_auto_stake_sweep()
        summary["ok"] = True
        summary.setdefault("error", None)
        return summary
    except Exception as exc:  # noqa: BLE001 — never sink the tick
        _LOG.warning("control_tick auto-stake failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "enabled": False,
            "scanned": 0,
            "staked": 0,
            "skipped": 0,
            "failed": 0,
            "results": [],
            "error": f"auto_stake_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_stalled_revival(task_id: str) -> dict[str, Any]:
    """ACT stage - revive existing opportunities stuck at stage=new past the
    freshness threshold.

    Complements auto_stake: auto_stake CREATES opportunities from newly
    qualified prospects; stalled_revival ADVANCES existing opportunities that
    the cash engine hasn't walked. See backend/cash_engine/stalled_revival.py
    for the 2026-07-07 diagnosis that motivated this stage. Fail-soft; a
    fault degrades the tick's tally rather than sinking the loop.
    """
    try:
        from backend.cash_engine.stalled_revival import run_stalled_revival_sweep

        summary = run_stalled_revival_sweep()
        summary["ok"] = True
        summary.setdefault("error", None)
        return summary
    except Exception as exc:  # noqa: BLE001 — never sink the tick
        _LOG.warning("control_tick stalled_revival failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "enabled": False,
            "scanned": 0,
            "revived": 0,
            "skipped": 0,
            "results": [],
            "error": f"stalled_revival_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_idle_drive_stage(task_id: str) -> dict[str, Any]:
    """ACT stage — the self-pacing engine. Observe whether production has gone
    idle during business hours and, if armed + behind pace, progress it through
    the governed channels.

    See backend/cash_engine/idle_production.py. Fail-soft: run_idle_drive never
    raises and always returns ok=True, but the import is still guarded so a
    packaging fault can't sink the tick. When the arming flag is OFF this is an
    observe-only no-op that returns ``{"enabled": False, "produced": False}``,
    surfaced so the operator sees at a glance whether self-pacing is armed.
    """
    try:
        from backend.cash_engine.idle_production import run_idle_drive

        return run_idle_drive()
    except Exception as exc:  # noqa: BLE001 — never sink the tick
        _LOG.warning("control_tick idle-drive failed task=%s: %s", task_id, exc)
        return {
            "ok": True,
            "enabled": False,
            "produced": False,
            "reason": f"idle_drive_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_stale_sweep(task_id: str) -> dict[str, Any]:
    """Best-effort CRM stale-deal sweep + cash-engine bridge stage."""
    try:
        from backend.crm.stale_sweep import sweep_and_alert

        result = sweep_and_alert()
        return {
            "ok": not result.error,
            "stale_count": result.count,
            "scanned": result.scanned,
            "error": result.error or None,
        }
    except Exception as exc:  # noqa: BLE001 - best-effort: never sink the tick
        _LOG.warning("control_tick stale sweep failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "stale_count": 0,
            "scanned": 0,
            "error": f"stale_sweep_failed: {exc.__class__.__name__}: {exc}",
        }


def _run_growth_tick(task_id: str) -> dict[str, Any]:
    """Best-effort GROWTH stage — customer genome refresh + campaign brief generation.

    Rebuilds per-customer behavioral segments from CustomerStore + Stripe,
    persists the updated genome JSON, then asks the campaign graph for the
    next UCB-ranked content briefs. All writes are idempotent; Stripe and
    Neo4j failures degrade gracefully to empty fields rather than sinking
    the tick.
    """
    try:
        import time as _time
        from backend.growth.customer_genome import CustomerGenomeEngine
        from backend.growth.campaign_graph import CampaignGraphEngine
        from backend.common.config import get_settings

        settings = get_settings()
        stripe_key = getattr(settings, "stripe_api_key", "") or ""

        genome_engine = CustomerGenomeEngine(stripe_api_key=stripe_key)
        t0 = _time.monotonic()
        summary = genome_engine.refresh(limit=100)
        genome_ms = int((_time.monotonic() - t0) * 1000)

        graph_engine = CampaignGraphEngine()
        briefs = graph_engine.suggest_next_batch(
            signals={"by_segment": summary.by_segment},
            batch_size=3,
        )
        graph_stats = graph_engine.graph_stats()

        return {
            "ok": True,
            "genome_total": summary.total,
            "genome_by_segment": summary.by_segment,
            "genome_refresh_ms": genome_ms,
            "content_briefs_suggested": len(briefs),
            "top_brief": (
                {
                    "node_id": briefs[0].node_id,
                    "objective": briefs[0].objective,
                    "channel": briefs[0].channel,
                    "angle": briefs[0].angle,
                    "keywords": briefs[0].primary_keywords[:3],
                }
                if briefs
                else {}
            ),
            "graph": graph_stats,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — best-effort: never sink the tick
        _LOG.warning("control_tick growth tick failed task=%s: %s", task_id, exc)
        return {
            "ok": False,
            "genome_total": 0,
            "genome_by_segment": {},
            "genome_refresh_ms": 0,
            "content_briefs_suggested": 0,
            "top_brief": {},
            "graph": {},
            "error": f"growth_tick_failed: {exc.__class__.__name__}: {exc}",
        }


def run_control_tick(
    *,
    task_id: str = "",
    entropy_inputs: dict[str, Any] | None = None,
    workcells: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one stack-level control-loop tick and return the combined snapshot.

    Parameters
    ----------
    task_id
        Optional caller-supplied id for correlation across the entropy +
        rebalance task-state rows. A synthetic ``ct-<uuid>`` id is generated
        when omitted.
    entropy_inputs
        Optional EntropyScanRequest-shaped dict of observed instability
        signals. Omitted -> an all-zero (stable) baseline scan.
    workcells
        Optional list of WorkcellInput-shaped dicts for the rebalance.
        Omitted/empty -> portfolio_controller's DEFAULT_TRACKED_WORKCELLS.

    Returns
    -------
    dict
        ``{"task_id", "ts", "ok", "entropy": {...}, "portfolio": {...},
           "recommendations": {...}, "recorded": bool, "enforcement_note"}``

        ``ok`` is True only when BOTH sub-calls succeeded. A partial tick
        (one stage failed) still returns 200 with that stage's ``error``
        populated and ``ok=False``.

    Never raises — both sub-calls are wrapped and the ledger write is
    best-effort. Safe to call on any cadence.
    """
    tid = task_id or f"ct-{uuid.uuid4().hex[:12]}"
    ts = iso_now()

    entropy_stage = _run_entropy_scan(tid, entropy_inputs or _DEFAULT_ENTROPY_INPUTS)
    portfolio_stage = _run_rebalance(tid, workcells or [])
    enforcement_stage = _run_enforcement(tid, portfolio_stage)
    auto_stake_stage = _run_auto_stake(tid)
    stalled_revival_stage = _run_stalled_revival(tid)
    idle_drive_stage = _run_idle_drive_stage(tid)
    crm_stage = _run_stale_sweep(tid)
    growth_stage = _run_growth_tick(tid)

    overall_ok = bool(
        entropy_stage["ok"]
        and portfolio_stage["ok"]
        and enforcement_stage["ok"]
        and auto_stake_stage["ok"]
        and stalled_revival_stage["ok"]
        and idle_drive_stage["ok"]
    )

    # Roll the actionable signal up to the top level so an operator does not
    # have to dig into nested stages: the instability verdict + the quota /
    # priority changes the loop is recommending this pass.
    recommendations = {
        "stable": entropy_stage["stable"],
        "entropy_score": entropy_stage["entropy_score"],
        "countermeasures": list(entropy_stage["countermeasures"]),
        "quota_cuts": portfolio_stage["quota_cuts"],
        "priority_boosts": portfolio_stage["priority_boosts"],
        # Only the workcells where a rule fired — the operator-actionable set.
        "workcell_adjustments": [
            adj
            for adj in portfolio_stage["adjustments"]
            if adj["quota_cut"] or adj["priority_boosted"]
        ],
        "customer_segments": growth_stage["genome_by_segment"],
        "top_content_brief": growth_stage["top_brief"],
    }

    snapshot: dict[str, Any] = {
        "task_id": tid,
        "ts": ts,
        "ok": overall_ok,
        "entropy": entropy_stage,
        "portfolio": portfolio_stage,
        "enforcement": enforcement_stage,
        "auto_stake": auto_stake_stage,
        "stalled_revival": stalled_revival_stage,
        "idle_drive": idle_drive_stage,
        "crm": crm_stage,
        "growth": growth_stage,
        "recommendations": recommendations,
        "enforcement_note": ENFORCEMENT_FOLLOWUP,
    }

    # RECORD — best-effort. A ledger failure is surfaced as recorded=False but
    # never fails the tick; the operator still gets the live snapshot.
    recorded = control_tick_ledger.record_tick(snapshot)
    snapshot["recorded"] = recorded

    _LOG.info(
        "control_tick task=%s ok=%s entropy_score=%s stable=%s "
        "quota_cuts=%d priority_boosts=%d enforced=%d genome_total=%d briefs=%d recorded=%s",
        tid,
        overall_ok,
        entropy_stage["entropy_score"],
        entropy_stage["stable"],
        portfolio_stage["quota_cuts"],
        portfolio_stage["priority_boosts"],
        enforcement_stage["applied"],
        growth_stage["genome_total"],
        growth_stage["content_briefs_suggested"],
        recorded,
    )
    return snapshot


__all__ = ["run_control_tick", "ENFORCEMENT_FOLLOWUP"]
