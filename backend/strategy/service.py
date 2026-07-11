"""Strategy service — orchestrates engine + CRM client + dispatcher."""

from __future__ import annotations

import logging
from typing import Any

from . import crm_client, dispatcher, engine, portfolio_manager
from .models import (
    DispatchRequest,
    EvaluateRequest,
    EvaluateResponse,
    RecordOutcomeRequest,
    RecordOutcomeResponse,
)
from .reward_density import RewardSignal

_LOG = logging.getLogger("samus.strategy.service")

# --- reward-signal derivation (strategy-integration build, Unit 3) ---------
# CRM hands strategy a 0-100 SEO audit score; RewardSignal.seo_score expects
# [0,1]. Divide by this to normalise. A defensive clamp in compute_reward_density
# keeps an out-of-range value bounded either way.
_SEO_SCORE_SCALE: float = 100.0
# A confirmed contact channel (an owner email) is the contactability signal we
# actually have at close time; without it we pass the documented neutral 0.5.
# RewardSignal.contactability / infrastructure_health are "derived" fields the
# caller blends — at the CRM->strategy boundary the only enrichment booleans
# available are owner_email / social_*, so contactability is derived from
# owner_email and infrastructure_health stays neutral (no infra telemetry
# crosses this boundary; the bandit weights it at 0.20 either way).
_CONTACTABILITY_WITH_EMAIL: float = 1.0
_CONTACTABILITY_NEUTRAL: float = 0.5
_INFRASTRUCTURE_HEALTH_NEUTRAL: float = 0.5


def _reward_signal_from_request(req: RecordOutcomeRequest) -> RewardSignal:
    """Build a :class:`RewardSignal` from a CRM-dispatched outcome request.

    Derivation (documented so the bandit's reward is reproducible):
      - ``outcome``               — the graded reward straight from the request
                                    (closed_won -> 1.0, closed_lost -> 0.0).
      - ``owner_email`` / ``social_facebook`` / ``social_instagram`` — the REAL
        enrichment booleans, passed through unchanged.
      - ``seo_score``             — the CRM 0-100 audit value scaled to [0,1].
      - ``contactability``        — DERIVED: 1.0 when an owner email was found
                                    (a reachable decision-maker), else the
                                    neutral 0.5. owner_email is the only
                                    contact-reach signal at this boundary.
      - ``infrastructure_health`` — neutral 0.5: no site/infra telemetry
                                    crosses the CRM->strategy boundary.
      - ``token_cost_usd``        — REAL per-prospect LLM spend captured during
                                    discovery (strategy-integration build,
                                    Unit 4) when the request carries it
                                    (> 0.0); otherwise left at the
                                    RewardSignal neutral default. The
                                    request's 0.0 means "no discovery-cost
                                    provenance", which is NOT the same as "a
                                    free call" — so it must not override the
                                    neutral default with a literal zero.
      - estimated_close_probability / latency — left at their documented
        neutral defaults (pending dedicated telemetry).
    """
    contactability = _CONTACTABILITY_WITH_EMAIL if req.owner_email else _CONTACTABILITY_NEUTRAL
    signal_fields: dict[str, Any] = dict(
        outcome=float(req.outcome),
        owner_email=req.owner_email,
        social_facebook=req.social_facebook,
        social_instagram=req.social_instagram,
        seo_score=float(req.seo_score) / _SEO_SCORE_SCALE,
        contactability=contactability,
        infrastructure_health=_INFRASTRUCTURE_HEALTH_NEUTRAL,
    )
    # Only override RewardSignal's neutral token-cost default when the request
    # carries a real, positive discovery-cost figure. A 0.0 means the deal had
    # no captured discovery cost (a pre-this-feature Opportunity, or a manual
    # operator deal) — leave the documented neutral default in place.
    if req.token_cost_usd > 0.0:
        signal_fields["token_cost_usd"] = float(req.token_cost_usd)
    return RewardSignal(**signal_fields)


async def evaluate(req: EvaluateRequest) -> EvaluateResponse:
    """Build context from CRM, run engine, return decision."""
    ctx = await crm_client.build_context(req.prospect_id)
    decision = engine.StrategyEngine().evaluate(ctx)
    return EvaluateResponse(
        prospect_id=req.prospect_id,
        action=decision.get("action", "none"),
        score=float(decision.get("score", 0.0)),
        reason=decision.get("reason", ""),
    )


async def dispatch_for_prospect(req: DispatchRequest) -> dict[str, Any]:
    """Build context from CRM and dispatch the requested action via gateway."""
    ctx = await crm_client.build_context(req.prospect_id)
    decision: dict[str, Any] = {"action": req.action}
    return await dispatcher.dispatch_strategy_action(decision, ctx)


async def record_outcome(req: RecordOutcomeRequest) -> RecordOutcomeResponse:
    """Update pattern-learning weights AND the hierarchical bandit on a deal outcome.

    Two effects:
      1. Heuristic pattern weights — KEPT unchanged from the legacy behaviour
         (``engine.boost_pattern`` / ``engine.penalize_pattern``).
      2. Hierarchical bandit — NEW (strategy-integration build, Unit 3): when
         the request carries both ``industry`` and ``policy_family`` (a CRM
         deal that closed against a known bandit arm) a reward-density
         :class:`RewardSignal` is built and
         ``portfolio_manager.update_policy_bandit`` records the trial — closing
         the decide->learn loop. A request WITHOUT industry/policy_family (a
         pre-this-feature Opportunity, or the legacy ``prospect_id``+``won``
         contract) falls back to effect 1 only — no bandit call.
    """
    if req.won:
        engine.boost_pattern("similar_prospects")
    else:
        engine.penalize_pattern("strategy_path")

    bandit_updated = False
    if req.industry and req.policy_family:
        signal = _reward_signal_from_request(req)
        portfolio_manager.update_policy_bandit(
            req.industry,
            req.policy_family,
            float(req.outcome),
            reward_signal=signal,
        )
        bandit_updated = True

    _LOG.info(
        "record_outcome prospect_id=%s won=%s industry=%s policy_family=%s "
        "outcome=%.3f bandit_updated=%s",
        req.prospect_id,
        req.won,
        req.industry,
        req.policy_family,
        float(req.outcome),
        bandit_updated,
    )
    return RecordOutcomeResponse(prospect_id=req.prospect_id, recorded=True)
