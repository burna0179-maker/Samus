"""FastAPI application for the strategy workcell."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import SERVICE_CAPABILITIES, check_capability
from backend.common.models import TaskEnvelope

from .models import (
    DispatchRequest,
    EvaluateRequest,
    EvaluateResponse,
    RecordOutcomeRequest,
    RecordOutcomeResponse,
)
from .service import dispatch_for_prospect, evaluate, record_outcome
from . import crm_client
from .optimizer import ProspectSignals, rank_portfolio
from .portfolio_manager import (
    PortfolioState,
    propose_allocation,
    update_bandit,
)
from .momentum_tracker import IndustryForecast
from .predictive_allocator import closer_mode_for, forecast_score
from .policy_compiler import POLICY_FAMILIES, build_execution_profile
from .portfolio_manager import (
    HIERARCHICAL_ARM_SEP,
    _ucb1_score,
    get_bandit_stats,
    get_policy_bandit_stats,
)

_LOG = logging.getLogger("samus.strategy.app")

_SERVICE = "strategy"

# Register the strategy bandit-observability capability at import time. The
# core ``capabilities.py`` registry is owned elsewhere; a workcell extends its
# own capability surface here via ``setdefault().update()`` — the same pattern
# the feedback workcell uses (``backend/feedback/app.py``). Idempotent.
SERVICE_CAPABILITIES.setdefault(_SERVICE, set()).add("read_bandit_stats")


def _forecast_allocation(raw_forecasts: list) -> dict:
    """Build IndustryForecasts, score them, and recommend a closer mode.

    Pure-logic — no LLM call. Each entry is fed into :class:`IndustryForecast`;
    invalid entries raise HTTP 422. Returns per-vertical forecast scores plus
    the Phase-2 ``closer_mode`` interface for the top-scoring vertical.
    """
    forecasts: list[IndustryForecast] = []
    for entry in raw_forecasts:
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail="each forecast must be a dict",
            )
        try:
            forecasts.append(IndustryForecast(**entry))
        except TypeError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid forecast entry: {exc}",
            ) from exc

    scored = [
        {
            "vertical": f.vertical,
            "forecast_score": forecast_score(f),
            "closer_mode": closer_mode_for(forecast_score(f)),
        }
        for f in forecasts
    ]
    top_score = max((s["forecast_score"] for s in scored), default=0.0)
    _LOG.info(
        "forecast_allocation: scored %d verticals top=%.4f",
        len(scored),
        top_score,
    )
    return {
        "forecasts": scored,
        "top_forecast_score": top_score,
        "recommended_closer_mode": closer_mode_for(top_score),
    }


def _compile_policy(payload: dict) -> dict:
    """Compile a deterministic CloserExecutionProfile from request inputs.

    Pure-logic — no LLM call. Reads ``vertical`` plus the forecast/density
    inputs from ``payload`` and returns the profile as a dict. Missing numeric
    inputs default to 0.0; a missing/unknown ``vertical`` is handled by the
    compiler's generic fallback.
    """
    vertical = payload.get("vertical")
    if not isinstance(vertical, str) or not vertical:
        raise HTTPException(status_code=422, detail="vertical is required")

    def _num(key: str) -> float:
        raw = payload.get(key, 0.0)
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be numeric: {exc}",
            ) from exc

    profile = build_execution_profile(
        forecast_score=_num("forecast_score"),
        reward_density=_num("reward_density"),
        regret_per_token=_num("regret_per_token"),
        enrichment_confidence=_num("enrichment_confidence"),
        vertical=vertical,
    )
    _LOG.info(
        "compile_policy: vertical=%s intensity=%s depth=%s confidence=%.4f",
        profile.vertical,
        profile.outreach_intensity,
        profile.proposal_depth,
        profile.confidence_score,
    )
    return {
        "vertical": profile.vertical,
        "allocation_weight": profile.allocation_weight,
        "outreach_intensity": profile.outreach_intensity,
        "followup_interval_hours": profile.followup_interval_hours,
        "escalation_threshold": profile.escalation_threshold,
        "max_token_budget_usd": profile.max_token_budget_usd,
        "proposal_depth": profile.proposal_depth,
        "channel_priority": profile.channel_priority,
        "template_family": profile.template_family,
        "retry_policy": profile.retry_policy,
        "confidence_score": profile.confidence_score,
    }


def _arm_view(arm_id: str, row: dict, total: int) -> dict:
    """Project one bandit arm row into a UCB1-annotated observability record.

    Pure-logic — no I/O. ``total`` is the sum-of-trials denominator UCB1 uses
    for its ``ln(total)`` exploration term; it is passed in so every arm in a
    snapshot is scored against the same denominator. An unplayed arm
    (``trials == 0``) gets a ``ucb1_score`` of ``inf`` and a ``mean_reward``
    of ``0.0`` — that is the documented "explore me first" signal, not a bug.
    """
    wins = float(row.get("wins", 0.0))
    trials = int(row.get("trials", 0))
    return {
        "arm_id": arm_id,
        "wins": wins,
        "trials": trials,
        "mean_reward": (wins / trials) if trials > 0 else 0.0,
        "ucb1_score": _ucb1_score(wins, trials, total),
    }


def _bandit_stats(industry: str | None) -> dict:
    """Build the bandit observability snapshot.

    With no ``industry`` the snapshot covers every arm (flat industry arms and
    hierarchical ``industry::policy_family`` arms alike) plus the global
    ``total_trials`` denominator. With an ``industry`` the snapshot is scoped
    to that industry's hierarchical policy-family arms, and ``policy_families``
    lists the families ``policy_compiler`` defines for the vertical so an
    operator can see which arms exist vs. which are still unplayed.

    Read-only: this surfaces the reward-density learning state — it never
    mutates the bandit. UCB1 scores are computed against the global
    sum-of-trials so the numbers match what ``select_best_policy`` sees.
    """
    flat_stats = get_bandit_stats()
    total_trials = sum(int(row.get("trials", 0)) for row in flat_stats.values())

    if industry:
        scoped = get_policy_bandit_stats(industry)
        arms = [_arm_view(arm_id, row, total_trials) for arm_id, row in sorted(scoped.items())]
        families = list(POLICY_FAMILIES.get(industry, ()))
        _LOG.info(
            "bandit_stats: industry=%s arms=%d total_trials=%d",
            industry,
            len(arms),
            total_trials,
        )
        return {
            "scope": "industry",
            "industry": industry,
            "total_trials": total_trials,
            "policy_families": families,
            "arms": arms,
        }

    arms = [_arm_view(arm_id, row, total_trials) for arm_id, row in sorted(flat_stats.items())]
    flat = [a for a in arms if HIERARCHICAL_ARM_SEP not in a["arm_id"]]
    hierarchical = [a for a in arms if HIERARCHICAL_ARM_SEP in a["arm_id"]]
    _LOG.info(
        "bandit_stats: all arms flat=%d hierarchical=%d total_trials=%d",
        len(flat),
        len(hierarchical),
        total_trials,
    )
    return {
        "scope": "all",
        "total_trials": total_trials,
        "flat_arms": flat,
        "hierarchical_arms": hierarchical,
    }


def create_app() -> object:
    """Build and return the strategy FastAPI application."""
    _app = create_base_app(service_name=_SERVICE)

    @_app.post("/strategy/evaluate", response_model=EvaluateResponse)
    async def evaluate_endpoint(req: EvaluateRequest) -> EvaluateResponse:
        """Evaluate a prospect and return the recommended action. Capability: ``evaluate``."""
        check_capability(_SERVICE, "evaluate")
        return await evaluate(req)

    @_app.post("/strategy/dispatch")
    async def dispatch_endpoint(req: DispatchRequest) -> dict:
        """Dispatch a strategy action for a prospect via gateway. Capability: ``dispatch_strategy_action``."""
        check_capability(_SERVICE, "dispatch_strategy_action")
        return await dispatch_for_prospect(req)

    @_app.post("/strategy/record-outcome", response_model=RecordOutcomeResponse)
    async def record_outcome_endpoint(req: RecordOutcomeRequest) -> RecordOutcomeResponse:
        """Record a deal outcome to update pattern weights. Capability: ``record_outcome``."""
        check_capability(_SERVICE, "record_outcome")
        return await record_outcome(req)

    @_app.post("/strategy/forecast-allocation")
    async def forecast_allocation_endpoint(req: dict) -> dict:
        """Score industry forecasts + recommend a closer mode. Capability: ``forecast_allocation``.

        Pure-logic — no LLM call. Body: ``{"forecasts": [<IndustryForecast dict>, ...]}``.
        """
        check_capability(_SERVICE, "forecast_allocation")
        raw_forecasts = (req or {}).get("forecasts")
        if not isinstance(raw_forecasts, list):
            raise HTTPException(status_code=422, detail="forecasts must be a list")
        return _forecast_allocation(raw_forecasts)

    @_app.get("/strategy/bandit-stats")
    async def bandit_stats_endpoint(industry: str | None = None) -> dict:
        """Read-only snapshot of the UCB1 bandit arms. Capability: ``read_bandit_stats``.

        Surfaces the reward-density learning state so it is observable:
        per-arm wins/trials, mean reward, and live UCB1 score. With no
        ``industry`` query param every arm is returned, split into
        ``flat_arms`` and ``hierarchical_arms``. With ``?industry=<vertical>``
        the response is scoped to that vertical's policy-family arms plus the
        ``policy_families`` the policy compiler defines for it. Pure read — it
        never mutates the bandit.
        """
        check_capability(_SERVICE, "read_bandit_stats")
        return _bandit_stats(industry)

    @_app.post("/work")
    async def work_endpoint(envelope: TaskEnvelope) -> dict:
        """SQS-style envelope dispatcher.

        Supported actions: evaluate, dispatch_strategy_action, record_outcome,
        build_context, rank_portfolio, propose_allocation, update_bandit_arm,
        forecast_allocation, compile_policy, read_bandit_stats.
        """
        action = envelope.metadata.get("action") or envelope.payload.get("action", "")
        payload = envelope.payload or {}

        try:
            if action == "evaluate":
                check_capability(_SERVICE, "evaluate")
                req = EvaluateRequest(**payload)
                result = await evaluate(req)
                return result.model_dump()

            if action == "dispatch_strategy_action":
                check_capability(_SERVICE, "dispatch_strategy_action")
                req = DispatchRequest(**payload)
                return await dispatch_for_prospect(req)

            if action == "record_outcome":
                check_capability(_SERVICE, "record_outcome")
                req = RecordOutcomeRequest(**payload)
                result = await record_outcome(req)
                return result.model_dump()

            if action == "build_context":
                check_capability(_SERVICE, "build_context")
                prospect_id = payload.get("prospect_id", "")
                if not prospect_id:
                    raise HTTPException(status_code=422, detail="prospect_id is required")
                ctx = await crm_client.build_context(prospect_id)
                return {
                    "prospect_id": ctx.prospect_id,
                    "lead_score": ctx.lead_score,
                    "seo_score": ctx.seo_score,
                    "stage": ctx.stage,
                    "engagement": ctx.engagement,
                    "last_activity": ctx.last_activity,
                    "conversion_signals": ctx.conversion_signals,
                }

            if action == "rank_portfolio":
                check_capability(_SERVICE, "rank_portfolio")
                raw_prospects = payload.get("prospects")
                if not isinstance(raw_prospects, list):
                    raise HTTPException(status_code=422, detail="prospects must be a list")
                top_tier = payload.get("top_tier", 5)
                mid_tier = payload.get("mid_tier", 15)
                signals = []
                for entry in raw_prospects:
                    try:
                        signals.append(ProspectSignals(**entry))
                    except TypeError as exc:
                        raise HTTPException(
                            status_code=422,
                            detail=f"invalid prospect entry: {exc}",
                        ) from exc
                rankings = rank_portfolio(signals, top_tier=top_tier, mid_tier=mid_tier)
                _LOG.info(
                    "rank_portfolio: scored %d prospects → %d actions",
                    len(signals),
                    len(rankings),
                )
                return {"ranked": [{"prospect_id": pid, "action": act} for pid, act in rankings]}

            if action == "propose_allocation":
                check_capability(_SERVICE, "propose_allocation")
                state_raw = payload.get("portfolio_state")
                if not isinstance(state_raw, dict):
                    raise HTTPException(status_code=422, detail="portfolio_state must be a dict")
                market_signals = payload.get("market_signals") or {}
                try:
                    state = PortfolioState(**state_raw)
                except TypeError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"invalid portfolio_state: {exc}",
                    ) from exc
                decision = propose_allocation(state, market_signals)
                _LOG.info(
                    "propose_allocation: priorities=%d deprioritize=%d actions=%d parse_error=%s",
                    len(decision.priorities),
                    len(decision.deprioritize),
                    len(decision.actions),
                    decision.parse_error,
                )
                return {
                    "priorities": decision.priorities,
                    "deprioritize": decision.deprioritize,
                    "actions": decision.actions,
                    "parse_error": decision.parse_error,
                }

            if action == "update_bandit_arm":
                check_capability(_SERVICE, "update_bandit_arm")
                arm_id = payload.get("arm_id")
                if not arm_id:
                    raise HTTPException(status_code=422, detail="arm_id is required")
                outcome_val = payload.get("outcome")
                if outcome_val is None:
                    raise HTTPException(status_code=422, detail="outcome is required")
                try:
                    outcome_float = float(outcome_val)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"outcome must be numeric: {exc}",
                    ) from exc
                update_bandit(arm_id, outcome_float)
                _LOG.info("update_bandit_arm: arm=%s outcome=%.4f", arm_id, outcome_float)
                return {"updated": True, "arm_id": arm_id, "outcome": outcome_float}

            if action == "forecast_allocation":
                check_capability(_SERVICE, "forecast_allocation")
                raw_forecasts = payload.get("forecasts")
                if not isinstance(raw_forecasts, list):
                    raise HTTPException(
                        status_code=422,
                        detail="forecasts must be a list",
                    )
                return _forecast_allocation(raw_forecasts)

            if action == "compile_policy":
                check_capability(_SERVICE, "compile_policy")
                return _compile_policy(payload)

            if action == "read_bandit_stats":
                check_capability(_SERVICE, "read_bandit_stats")
                raw_industry = payload.get("industry")
                if raw_industry is not None and not isinstance(raw_industry, str):
                    raise HTTPException(
                        status_code=422,
                        detail="industry must be a string",
                    )
                return _bandit_stats(raw_industry or None)

        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")

    return _app


app = create_app()
