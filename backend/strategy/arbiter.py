"""Economic task arbiter — one ranked queue for the day's candidate work.

HOTL Tranche 2 (Economic Brain). Every candidate action — call prospect X,
email Y, run an SEO audit on Z, retention-touch W, follow up with V — becomes
a :class:`WorkBid` scored by the shared priority formula
(:func:`backend.crm.scoring.compute_priority`)::

    priority = (ev_usd * probability * urgency)
               / (time_estimate_hrs * max(cost_usd, floor))

The arbiter does not *replace* the sources that propose work (CRM lifecycle
still proposes operator tasks, CallState still schedules follow-ups); it
ranks the union of their proposals so the operator surface (morning brief,
``GET /admin/arbitration``) and autonomous dispatch consume ONE economically
ordered queue instead of N per-source queues.

Bid collectors are all best-effort: a missing data source (no AWS, empty
table, unreadable ledger) degrades to an empty list, never an exception.

Every :func:`arbitrate_daily` run is persisted to an append-only JSONL
ledger (``SAMUS_ARBITRATION_LOG``, ``open_ledger`` backend-switched like
every other Samus ledger) and emitted as a ``decision.made`` business event
through :mod:`backend.common.business_events_shim` (a no-op until the
Tranche-1 unified stream merges).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from backend.common import persistence
from backend.common.business_events_shim import emit_business_event
from backend.common.dates import iso_now
from backend.crm.scoring import (
    DEFAULT_TIME_ESTIMATE_HRS,
    EMAIL_SEND_COST_USD,
    compute_priority,
    estimate_voice_cost_usd,
    priority_score,
    urgency_multiplier,
)

_LOG = logging.getLogger("samus.strategy.arbiter")

# JSONL arbitration ledger (one record per arbitrate_daily run).
_DEFAULT_LEDGER_PATH = "/opt/samus/data/strategy/arbitration.jsonl"

# Conservative defaults for bids whose source carries no economics of its
# own (e.g. an operator task not linked to an opportunity).
DEFAULT_BID_EV_USD: float = 100.0
DEFAULT_BID_PROBABILITY: float = 0.30

# How many ranked bids the operator surfaces (brief / route) show by default.
TOP_N: int = 10


def _ledger():
    return persistence.open_ledger(
        jsonl_path=os.getenv("SAMUS_ARBITRATION_LOG", _DEFAULT_LEDGER_PATH),
        collection="strategy_arbitration",
    )


# ---------------------------------------------------------------------------
# WorkBid + ranking
# ---------------------------------------------------------------------------

@dataclass
class WorkBid:
    """One candidate unit of work, priced in the common economic currency."""

    action: str                       # e.g. "call_follow_up", "seo_audit"
    target_id: str                    # prospect / opportunity / task id
    ev_usd: float                     # expected value if the work converts
    probability: float                # 0-1 chance the work converts
    urgency: float                    # 0-1 time-decay multiplier
    cost_usd: float                   # marginal cost of doing the work
    time_estimate_hrs: float          # operator/agent time to execute
    source_workcell: str              # which workcell proposed the bid
    rationale: str = ""               # one-line human "why"
    channel: str = ""                 # call / email / seo / retention
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> float:
        return compute_priority(
            ev_usd=self.ev_usd,
            probability=self.probability,
            urgency=self.urgency,
            time_estimate_hrs=self.time_estimate_hrs,
            cost_usd=self.cost_usd,
        )

    def to_record(self) -> dict[str, Any]:
        rec = asdict(self)
        rec["priority"] = self.priority
        return rec


def rank_bids(bids: list[WorkBid]) -> list[WorkBid]:
    """Sort bids by priority, highest first. Stable for equal priorities.

    Ties break on EV (bigger deal first) so the ordering is deterministic
    for fixture tests.
    """
    return sorted(bids, key=lambda b: (b.priority, b.ev_usd), reverse=True)


# ---------------------------------------------------------------------------
# Bid collectors — each degrades to [] when its data source is unavailable
# ---------------------------------------------------------------------------

_TASK_KIND_CHANNEL: dict[str, str] = {
    "call": "call",
    "follow_up": "call",
    "email": "email",
    "outreach": "email",
    "retention": "retention",
    "seo": "seo",
}

# Map a WorkBid channel to the affordability cost tier that gates it (see
# backend/cash_engine/affordability.py). Slice C (2026-07-06): wired at
# arbitration time so a prospect-level bid can be held back when cash posture
# is conserve/lean, not just campaigns. Unknown channels default to "low" —
# moderate, permissive under lean+invest, held under conserve.
_CHANNEL_COST_TIER: dict[str, str] = {
    "seo": "free",
    "email": "low",
    "retention": "low",
    "call": "paid",
    "campaign": "low",       # a marketing campaign — cost tier per bid metadata
    "prospect": "low",       # advance a prospect via the baseline sales action
}


def _channel_cost_tier(bid: "WorkBid") -> str:
    """Resolve the cost tier for a bid's channel. Bid metadata may override.

    A campaign bid carries its own ``cost_tier`` in metadata (matching the
    Campaign dataclass field); that takes precedence so a "free"-tier voicemail
    campaign survives a conserve posture even though its channel label maps to
    "low" by default.
    """
    override = ""
    try:
        override = str((bid.metadata or {}).get("cost_tier", "") or "").strip()
    except Exception:  # noqa: BLE001
        override = ""
    if override:
        return override
    return _CHANNEL_COST_TIER.get(str(bid.channel or ""), "low")


def collect_crm_task_bids(*, limit: int = 50) -> list[WorkBid]:
    """Bids from open CRM operator tasks.

    A task linked to an opportunity inherits that opportunity's economics
    via :func:`priority_score`; an unlinked task gets the conservative
    defaults with urgency decaying from its ``created_at``.
    """
    try:
        from backend.crm import service as crm_service
        result = crm_service.list_operator_tasks(status="open", limit=limit)
        tasks = list(result.tasks)
    except Exception as exc:  # noqa: BLE001 — CRM is optional input
        _LOG.debug("collect_crm_task_bids degraded to empty: %s", exc)
        return []

    now = _dt.datetime.now(_dt.timezone.utc)
    bids: list[WorkBid] = []
    for task in tasks:
        kind = str(getattr(task, "kind", "") or "other")
        channel = _TASK_KIND_CHANNEL.get(kind, "email")
        ev = DEFAULT_BID_EV_USD
        probability = DEFAULT_BID_PROBABILITY
        urgency = 1.0
        cost = EMAIL_SEND_COST_USD
        hours = DEFAULT_TIME_ESTIMATE_HRS
        opp = None
        if getattr(task, "related_entity_kind", "") == "opportunity" \
                and getattr(task, "related_entity_id", ""):
            try:
                from backend.crm import service as crm_service
                opp = crm_service.get_opportunity(task.related_entity_id)
            except Exception:  # noqa: BLE001
                opp = None
        if opp is not None:
            score = priority_score(opp.model_dump(), now=now)
            ev = score.ev_usd
            probability = score.probability
            urgency = score.urgency
            cost = max(cost, score.cost_usd)
            hours = score.time_estimate_hrs
        else:
            created = str(getattr(task, "created_at", "") or "")
            days = 0.0
            if created:
                try:
                    parsed = _dt.datetime.fromisoformat(
                        created[:-1] + "+00:00" if created.endswith("Z") else created,
                    )
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
                    days = max(0.0, (now - parsed).total_seconds() / 86400.0)
                except ValueError:
                    pass
            urgency = urgency_multiplier(days)
        bids.append(WorkBid(
            action=f"operator_task:{kind}",
            target_id=task.operator_task_id,
            ev_usd=ev,
            probability=probability,
            urgency=urgency,
            cost_usd=cost,
            time_estimate_hrs=hours,
            source_workcell="crm",
            channel=channel,
            rationale=(task.title or kind)[:120],
            metadata={
                "related_entity_kind": getattr(task, "related_entity_kind", ""),
                "related_entity_id": getattr(task, "related_entity_id", ""),
            },
        ))
    return bids


def collect_follow_up_bids(*, today: str | None = None, limit: int = 100) -> list[WorkBid]:
    """Bids from due outreach follow-ups (CallState: emailed earlier, call now)."""
    try:
        from backend.crm import service as crm_service
        day = today or _dt.date.today().isoformat()
        result = crm_service.list_follow_ups_due(today=day, limit=limit)
        follow_ups = list(result.follow_ups)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("collect_follow_up_bids degraded to empty: %s", exc)
        return []

    bids: list[WorkBid] = []
    for fu in follow_ups:
        ev = DEFAULT_BID_EV_USD
        probability = DEFAULT_BID_PROBABILITY
        try:
            from backend.crm import service as crm_service
            opp = crm_service.get_opportunity_for_prospect(fu.prospect_id)
            if opp is not None:
                ev = float(opp.deal_size_usd or ev)
                probability = float(opp.close_probability or probability)
        except Exception:  # noqa: BLE001
            pass
        # A follow-up cools as the prospect waits — decay on days_waiting.
        urgency = urgency_multiplier(float(fu.days_waiting or 0))
        cost = estimate_voice_cost_usd(1) or 0.05  # one dial; nominal floor
        bids.append(WorkBid(
            action="call_follow_up",
            target_id=fu.prospect_id,
            ev_usd=ev,
            probability=probability,
            urgency=urgency,
            cost_usd=cost,
            time_estimate_hrs=0.25,
            source_workcell="voice",
            channel="call",
            rationale=f"outreach sent {fu.days_waiting}d ago — call due"
                      + (f" ({fu.company})" if fu.company else ""),
            metadata={"phone": fu.phone, "subject": fu.subject},
        ))
    return bids


def collect_seo_bids(*, limit: int = 50, max_score: int = 50) -> list[WorkBid]:
    """Bids to (re)run SEO audits on open opportunities with weak SEO signal.

    An opportunity whose ``seo_score`` is below ``max_score`` (including the
    never-audited 0) is an audit candidate — the audit is cheap, autonomous,
    and improves both the pitch and the enrichment depth.
    """
    try:
        from backend.crm import service as crm_service
        result = crm_service.list_opportunities(limit=limit)
        opportunities = list(result.opportunities)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("collect_seo_bids degraded to empty: %s", exc)
        return []

    now = _dt.datetime.now(_dt.timezone.utc)
    bids: list[WorkBid] = []
    for opp in opportunities:
        stage = str(opp.stage or "")
        if stage.startswith("closed"):
            continue
        seo_score = int(getattr(opp, "seo_score", 0) or 0)
        if seo_score >= max_score:
            continue
        score = priority_score(opp.model_dump(), now=now)
        bids.append(WorkBid(
            action="seo_audit",
            target_id=opp.prospect_id or opp.opportunity_id,
            ev_usd=score.ev_usd,
            # An audit only *assists* the close — haircut the probability.
            probability=round(score.probability * 0.5, 4),
            urgency=score.urgency,
            cost_usd=0.05,          # metered LLM summarisation + fetch
            time_estimate_hrs=0.25,  # autonomous; agent time only
            source_workcell="seo",
            channel="seo",
            rationale=f"seo_score={seo_score} (<{max_score}) on open deal "
                      f"in stage={stage}",
            metadata={"opportunity_id": opp.opportunity_id},
        ))
    return bids


def collect_reengagement_bids(*, window_days: int = 7) -> list[WorkBid]:
    """Bids from the re-engagement queue (soft-no prospects resurfaced).

    Reads the re-engagement sweep's queued ledger
    (``crm/reengagement_queued.jsonl``): every prospect queued within the
    window becomes a retention-touch bid. Missing ledger -> [].
    """
    try:
        from backend.crm.reengagement_sweep import _ledger_path
        path = _ledger_path()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("collect_reengagement_bids degraded to empty: %s", exc)
        return []
    if not path.exists():
        return []
    import json as _json
    cutoff = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    ).isoformat()
    seen: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue
                if str(rec.get("ts", "")) < cutoff:
                    continue
                pid = str(rec.get("prospect_id", "") or "")
                if pid:
                    seen[pid] = rec  # newest wins
    except OSError as exc:
        _LOG.debug("collect_reengagement_bids ledger read failed: %s", exc)
        return []

    bids: list[WorkBid] = []
    for pid, rec in seen.items():
        ev = DEFAULT_BID_EV_USD
        probability = 0.10  # soft-no re-engagement converts rarely
        try:
            from backend.crm import service as crm_service
            opp = crm_service.get_opportunity_for_prospect(pid)
            if opp is not None:
                ev = float(opp.deal_size_usd or ev)
        except Exception:  # noqa: BLE001
            pass
        bids.append(WorkBid(
            action="reengagement_touch",
            target_id=pid,
            ev_usd=ev,
            probability=probability,
            urgency=0.5,  # queued = pre-vetted eligible, but never same-day hot
            cost_usd=EMAIL_SEND_COST_USD,
            time_estimate_hrs=0.1,
            source_workcell="crm",
            channel="retention",
            rationale="soft-no prospect resurfaced by re-engagement sweep",
            metadata={"opportunity_id": rec.get("opportunity_id", "")},
        ))
    return bids


def collect_prospect_bids(*, limit: int = 50) -> list[WorkBid]:
    """Bids to advance each open opportunity via the baseline sales action.

    Slice C (2026-07-06): closes the "arbiter never sees prospects as a stream
    of their own" gap. The SEO collector already scans opportunities, but only
    to propose SEO audits; this collector proposes the primary sales action
    ("advance the prospect through its current stage") for the same set,
    priced with the canonical :func:`priority_score` economics so a hot deal
    can outrank an operator task or a follow-up on the same page.

    Skips closed-stage opportunities and any without a resolvable target id.
    """
    try:
        from backend.crm import service as crm_service
        result = crm_service.list_opportunities(limit=limit)
        opportunities = list(result.opportunities)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("collect_prospect_bids degraded to empty: %s", exc)
        return []

    now = _dt.datetime.now(_dt.timezone.utc)
    bids: list[WorkBid] = []
    for opp in opportunities:
        stage = str(opp.stage or "")
        if stage.startswith("closed"):
            continue
        target = opp.prospect_id or opp.opportunity_id
        if not target:
            continue
        score = priority_score(opp.model_dump(), now=now)
        bids.append(WorkBid(
            action="advance_prospect",
            target_id=target,
            ev_usd=score.ev_usd,
            probability=score.probability,
            urgency=score.urgency,
            cost_usd=score.cost_usd,
            time_estimate_hrs=score.time_estimate_hrs,
            source_workcell="crm",
            channel="email",  # baseline sales action is an outreach touch
            rationale=(
                f"open opportunity stage={stage} deal=${score.ev_usd:,.0f} "
                f"p={score.probability:.2f}"
            ),
            metadata={
                "opportunity_id": opp.opportunity_id,
                "stage": stage,
            },
        ))
    return bids


def collect_campaign_bids() -> list[WorkBid]:
    """Bids for each eligible marketing campaign from ``default_portfolio_deps``.

    Slice C (2026-07-06): campaigns and prospect-level work now rank on the
    same queue instead of in independent silos (see
    ``backend/cash_engine/campaign_portfolio.py``). Each eligible campaign
    becomes one bid whose channel = "campaign", ``cost_tier`` metadata carries
    the campaign's own tier (``free`` / ``low`` / ``paid``) so affordability
    gating is precise — a "free"-tier voicemail-draft campaign runs under
    conserve; a "paid" live-dial campaign is held.

    EV is estimated conservatively as
    ``default_cap × DEFAULT_BID_EV_USD × conversion_prob``: the campaign's
    default per-pass volume times a nominal $-per-touch times a coarse
    conversion probability. Priority-order across campaigns preserves each
    campaign's ``priority`` via a small urgency haircut for the lower-priority
    ones (so priority=1.2 outranks priority=1.0 at equal EV/prob/cost).

    Ineligible campaigns are omitted (they were never going to fire this
    tick). Missing / failing dependency chain -> empty list, never raises.
    """
    try:
        from backend.cash_engine.campaign_portfolio import default_portfolio_deps
        deps = default_portfolio_deps()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("collect_campaign_bids degraded to empty: %s", exc)
        return []

    bids: list[WorkBid] = []
    max_priority = max((c.priority for c in deps.campaigns), default=1.0) or 1.0
    for campaign in deps.campaigns:
        try:
            eligible = bool(campaign.is_eligible())
        except Exception:  # noqa: BLE001 — an eligibility probe fault -> skip
            eligible = False
        if not eligible:
            continue

        # Coarse conversion prior by campaign kind. Voice touches convert a bit
        # better than cold email; retention touches convert lower. These are
        # priors — the channel bandit + ROI ledger refine over time.
        kind = str(campaign.kind or "").lower()
        conversion_prob = 0.05
        if kind == "voice":
            conversion_prob = 0.08
        elif kind == "retention":
            conversion_prob = 0.03

        volume = max(1, int(campaign.default_cap or 1))
        # EV_per_pass = volume × nominal $-per-touch × conversion_prob.
        # Use DEFAULT_BID_EV_USD as the nominal $-per-touch anchor so the
        # magnitude sits in the same band as prospect bids.
        ev_usd = float(volume) * DEFAULT_BID_EV_USD * conversion_prob

        # Priority-preserving urgency: a lower-priority campaign gets a mild
        # haircut so ordering matches the priority field at equal economics.
        urgency = max(0.1, min(1.0, float(campaign.priority) / float(max_priority)))

        cost_usd = float(campaign.est_cost_usd or 0.0)
        # Never divide-by-zero the priority formula: give free-tier campaigns a
        # tiny nominal cost so the ratio stays finite AND ordered.
        cost_usd = max(cost_usd, 0.01)

        bids.append(WorkBid(
            action=f"campaign:{campaign.campaign_id}",
            target_id=campaign.campaign_id,
            ev_usd=ev_usd,
            probability=conversion_prob,
            urgency=urgency,
            cost_usd=cost_usd,
            time_estimate_hrs=0.5,
            source_workcell="cash_engine",
            channel="campaign",
            rationale=(
                f"campaign {campaign.campaign_id} eligible; kind={kind or '?'} "
                f"cap={volume} tier={campaign.cost_tier}"
            ),
            metadata={
                "campaign_id": campaign.campaign_id,
                "kind": kind,
                "cost_tier": str(campaign.cost_tier or "low"),
                "monitor_cost": float(campaign.monitor_cost or 0.0),
                "priority_hint": float(campaign.priority),
            },
        ))
    return bids


# ---------------------------------------------------------------------------
# Daily arbitration
# ---------------------------------------------------------------------------

@dataclass
class ArbitrationResult:
    """One arbitration run: the ranked queue plus per-bid decision records.

    Slice C (2026-07-06): additive fields ``affordability`` and
    ``held_by_affordability`` surface the cash posture applied to this run and
    the bids that were dropped from ``ranked`` because their channel's cost
    tier isn't in the current ``allowed_tiers``. Existing consumers reading
    ``ranked`` still see the exact same shape (a decision record per active
    bid). ``bid_count`` counts ONLY active bids, matching the historical
    semantics of "how many decisions were made this run".
    """

    day: str
    generated_at: str
    bid_count: int
    best_channel: str
    ranked: list[dict[str, Any]] = field(default_factory=list)
    # Additive Slice C fields.
    affordability: Optional[dict[str, Any]] = None
    held_by_affordability: list[dict[str, Any]] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def _decision_record(rank: int, bid: WorkBid) -> dict[str, Any]:
    """DecisionRecord-style rationale for one ranked bid (framework §4.2)."""
    return {
        "rank": rank,
        **bid.to_record(),
        "decision": {
            "why": bid.rationale or bid.action,
            "expected_outcome": (
                f"EV ${bid.ev_usd:,.2f} at p={bid.probability:.2f} "
                f"urgency={bid.urgency:.2f}"
            ),
            "data_used": [f"source_workcell={bid.source_workcell}",
                          f"channel={bid.channel}"],
            "cost_usd": bid.cost_usd,
            "time_estimate_hrs": bid.time_estimate_hrs,
            "priority": bid.priority,
        },
    }


def arbitrate_daily(
    bids: list[WorkBid] | None = None,
    *,
    day: str | None = None,
    persist: bool = True,
    apply_affordability: bool = True,
) -> ArbitrationResult:
    """Collect (or accept) the day's bids, rank them, gate by affordability,
    persist + emit.

    ``bids`` is injectable for tests / callers that already hold a bid set;
    when None the SIX collectors run (each degrading to empty): CRM operator
    tasks, due follow-ups, SEO audits, re-engagement touches, prospect
    advances, and eligible campaigns. The result is appended to the
    arbitration ledger and emitted as one ``decision.made`` business event
    (no-op pre-merge via the shim).

    Affordability gating (Slice C, 2026-07-06): when ``apply_affordability``
    is True (default) the current cash posture is read from
    :func:`backend.cash_engine.affordability.assess_affordability` and each
    ranked bid is classified by its channel's cost tier
    (see :data:`_CHANNEL_COST_TIER`, with a per-bid ``cost_tier`` metadata
    override). Bids whose tier is outside the posture's ``allowed_tiers`` are
    moved from ``ranked`` to ``held_by_affordability`` — visible in the
    result + the ledger, never silently dropped. Failure to read affordability
    degrades to no gating (permissive) rather than blocking every bid.

    ``bid_count`` counts only ACTIVE bids to preserve the historical
    "decisions made this run" semantics; the held count is available via
    ``len(result.held_by_affordability)``.
    """
    if bids is None:
        bids = (
            collect_crm_task_bids()
            + collect_follow_up_bids()
            + collect_seo_bids()
            + collect_reengagement_bids()
            + collect_prospect_bids()
            + collect_campaign_bids()
        )
    ranked_bids = rank_bids(bids)

    try:
        from backend.strategy.portfolio_manager import select_best_channel
        best_channel = select_best_channel()
    except Exception as exc:  # noqa: BLE001 — bandit is optional input
        _LOG.debug("arbitrate_daily: channel bandit unavailable: %s", exc)
        best_channel = ""

    # Read cash posture ONCE per run. Failure => permissive (allow every tier)
    # so a finance outage never turns arbitration into a blanket block. The
    # returned Affordability is still persisted in the result for auditability.
    affordability_dict: Optional[dict[str, Any]] = None
    allowed_tiers: Optional[frozenset] = None
    if apply_affordability:
        try:
            from backend.cash_engine.affordability import assess_affordability
            afford = assess_affordability()
            affordability_dict = afford.to_dict()
            allowed_tiers = afford.allowed_tiers
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "arbitrate_daily: affordability read failed (%s); permissive",
                exc,
            )

    # Split ranked bids into active vs held-by-affordability. The gating is
    # inclusive-fail-open: when allowed_tiers is None (no posture read) every
    # bid stays active. Ordering is preserved within each partition.
    active_bids: list[WorkBid] = []
    held_bids: list[WorkBid] = []
    for bid in ranked_bids:
        if allowed_tiers is None:
            active_bids.append(bid)
            continue
        tier = _channel_cost_tier(bid)
        if tier in allowed_tiers:
            active_bids.append(bid)
        else:
            held_bids.append(bid)

    def _held_record(rank: int, bid: WorkBid) -> dict[str, Any]:
        rec = _decision_record(rank, bid)
        rec["decision"]["held_reason"] = (
            f"channel_tier={_channel_cost_tier(bid)} not in "
            f"posture_allowed_tiers={sorted(allowed_tiers or [])}"
        )
        return rec

    result = ArbitrationResult(
        day=day or _dt.date.today().isoformat(),
        generated_at=iso_now(),
        bid_count=len(active_bids),
        best_channel=best_channel,
        ranked=[_decision_record(i + 1, b) for i, b in enumerate(active_bids)],
        affordability=affordability_dict,
        held_by_affordability=[
            _held_record(i + 1, b) for i, b in enumerate(held_bids)
        ],
    )

    if persist:
        try:
            _ledger().append({"kind": "arbitration", **result.to_record()})
        except Exception as exc:  # noqa: BLE001 — best-effort ledger
            _LOG.warning("arbitration ledger append failed: %s", exc)
        top = result.ranked[0] if result.ranked else None
        emit_business_event(
            "decision.made",
            workcell="strategy",
            metadata={
                "decision_kind": "daily_arbitration",
                "day": result.day,
                "bid_count": result.bid_count,
                "held_count": len(result.held_by_affordability),
                "best_channel": best_channel,
                "posture": (affordability_dict or {}).get("posture", "unknown"),
                "top_action": (top or {}).get("action", ""),
                "top_target": (top or {}).get("target_id", ""),
                "top_priority": (top or {}).get("priority", 0.0),
            },
        )
    return result


def latest_arbitration() -> dict[str, Any] | None:
    """Most recent persisted arbitration record, or None. Never raises."""
    try:
        rows = _ledger().tail(limit=1)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("latest_arbitration read failed: %s", exc)
        return None
    return rows[-1] if rows else None


__all__ = [
    "WorkBid",
    "ArbitrationResult",
    "rank_bids",
    "arbitrate_daily",
    "latest_arbitration",
    "collect_crm_task_bids",
    "collect_follow_up_bids",
    "collect_seo_bids",
    "collect_reengagement_bids",
    "collect_prospect_bids",
    "collect_campaign_bids",
    "TOP_N",
]
