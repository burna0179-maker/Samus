"""Reward-density scoring — the signal a predictive allocator maximises.

Replaces the reactive UCB1 scalar reward (1.0 conversion / 0.0 no-response)
with a *density* score that blends the conversion outcome with the durable
signals around it: enrichment depth, infrastructure health, SEO opportunity
gap, token efficiency and resolution latency.

Pure-logic — deterministic, no I/O, no network. The bandit
(:mod:`backend.strategy.portfolio_manager`) consumes ``compute_reward_density``
when a caller supplies a :class:`RewardSignal`; otherwise it stays on the
legacy scalar path (byte-identical behaviour).

Telemetry reconciliation
------------------------
``portfolio_manager``'s own docstring notes "live CRM conversion signals do
not yet exist". The fields below are tagged real vs neutral-default:

  - REAL: ``outcome``, ``owner_email``/``social_facebook``/``social_instagram``
    (from :mod:`backend.prospecting.enrichment`), ``seo_score``.
  - DERIVED-but-supplied: ``contactability``, ``infrastructure_health`` —
    callers compute these from the enrichment booleans / seo audit.
  - NEUTRAL-DEFAULT pending telemetry: ``estimated_close_probability``,
    ``latency_to_resolution_sec``, ``token_cost_usd``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "RewardSignal",
    "compute_reward_density",
    "ENRICHMENT_OWNER_EMAIL_WEIGHT",
    "ENRICHMENT_FACEBOOK_WEIGHT",
    "ENRICHMENT_INSTAGRAM_WEIGHT",
    "INFRASTRUCTURE_HEALTH_WEIGHT",
    "CONTACTABILITY_WEIGHT",
    "SEO_GAP_WEIGHT",
    "EFFICIENCY_WEIGHT",
    "LATENCY_PENALTY_WEIGHT",
    "LATENCY_SATURATION_SEC",
    "MIN_TOKEN_COST_USD",
    # --- G7 surface (ADR-004 reward = stages - cost - k*harm + stripe) -------
    "RewardComputation",
    "RewardComputationStore",
    "DefaultRewardComputationStore",
    "compute_reward",
    "RewardPersistenceError",
    "STAGE_ORDER",
    "STAGE_WEIGHT_ENV",
    "LLM_COST_WEIGHT_ENV",
    "HARM_K_ENV",
    "TERMINAL_MULTIPLIER_ENV",
    "DEFAULT_STAGE_WEIGHT",
    "DEFAULT_LLM_COST_WEIGHT",
    "DEFAULT_HARM_K",
    "DEFAULT_TERMINAL_MULTIPLIER",
]

_LOG = logging.getLogger("samus.strategy.reward_density")

# --- named coefficients (no magic numbers in the formula) ------------------
ENRICHMENT_OWNER_EMAIL_WEIGHT: float = 0.30
ENRICHMENT_FACEBOOK_WEIGHT: float = 0.20
ENRICHMENT_INSTAGRAM_WEIGHT: float = 0.10
INFRASTRUCTURE_HEALTH_WEIGHT: float = 0.20
CONTACTABILITY_WEIGHT: float = 0.20
SEO_GAP_WEIGHT: float = 0.15
EFFICIENCY_WEIGHT: float = 0.25
LATENCY_PENALTY_WEIGHT: float = 0.20
# Latency saturates the penalty at 1 day — past 24h the deal is cold either way.
LATENCY_SATURATION_SEC: float = 86_400.0
# Floor for the efficiency denominator so a zero token cost can't divide-by-zero.
MIN_TOKEN_COST_USD: float = 0.01


@dataclass
class RewardSignal:
    """Inputs to :func:`compute_reward_density` for one bandit trial.

    Every field degrades gracefully: the neutral-default fields can be
    omitted entirely and the density score stays well-defined.
    """

    outcome: float  # real — 1.0 conversion / 0.0 no-response
    owner_email: bool  # real — enrichment found an owner email
    social_facebook: bool  # real — enrichment found a Facebook handle
    social_instagram: bool  # real — enrichment found an Instagram handle
    seo_score: float  # real — seo audit score, expected [0,1]
    contactability: float  # derived — caller blends enrichment reach, [0,1]
    infrastructure_health: float  # derived — caller blends site/infra health, [0,1]
    estimated_close_probability: float = 0.5  # neutral-default pending CRM conversion telemetry
    latency_to_resolution_sec: float = 0.0  # neutral-default pending resolution-time telemetry
    token_cost_usd: float = 0.01  # neutral-default pending reliable per-arm cost telemetry


def _clamp01(value: float) -> float:
    """Clamp a value into [0.0, 1.0]; defensive against out-of-range inputs."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def compute_reward_density(signal: RewardSignal) -> float:
    """Blend a trial's outcome + durable signals into one density score.

    Formula (every coefficient is a named module constant)::

        enrichment_weight     = 0.30*owner_email + 0.20*facebook + 0.10*instagram
        infrastructure_weight = infrastructure_health*0.20 + contactability*0.20
        seo_gap_weight        = (1.0 - seo_score)*0.15
        efficiency_weight     = (close_prob / max(token_cost_usd, 0.01)) * 0.25
        latency_penalty       = min(latency_sec / 86400, 1.0) * 0.20
        density = outcome + enrichment + infrastructure + seo_gap
                  + efficiency - latency_penalty

    ``seo_score`` is expected in [0,1] and clamped defensively so a caller
    passing a 0-100 audit score still yields a bounded gap term.
    """
    enrichment_weight = (
        ENRICHMENT_OWNER_EMAIL_WEIGHT * int(signal.owner_email)
        + ENRICHMENT_FACEBOOK_WEIGHT * int(signal.social_facebook)
        + ENRICHMENT_INSTAGRAM_WEIGHT * int(signal.social_instagram)
    )

    infrastructure_weight = (
        _clamp01(signal.infrastructure_health) * INFRASTRUCTURE_HEALTH_WEIGHT
        + _clamp01(signal.contactability) * CONTACTABILITY_WEIGHT
    )

    # seo_score normalised to [0,1]; a high gap (low seo) is opportunity.
    seo_gap_weight = (1.0 - _clamp01(signal.seo_score)) * SEO_GAP_WEIGHT

    token_cost = max(signal.token_cost_usd, MIN_TOKEN_COST_USD)
    efficiency_weight = (signal.estimated_close_probability / token_cost) * EFFICIENCY_WEIGHT

    latency_fraction = min(signal.latency_to_resolution_sec / LATENCY_SATURATION_SEC, 1.0)
    latency_penalty = latency_fraction * LATENCY_PENALTY_WEIGHT

    return (
        signal.outcome
        + enrichment_weight
        + infrastructure_weight
        + seo_gap_weight
        + efficiency_weight
        - latency_penalty
    )


# ===========================================================================
# G7 — ADR-004 reward formula (stages - cost - k*harm + stripe terminal)
# ===========================================================================
#
# This block implements Guardrail G7 from `docs/codex/04_guardrails.md` and
# ADR-004 from `docs/codex/08_decisions_log.md`. It coexists with the legacy
# ``compute_reward_density`` density score above (the bandit's per-trial
# signal). G7's :func:`compute_reward` is the canonical, opportunity-level
# reward used for ADR-004's intended bandit credit signal — see the parent
# notes for the migration plan.
#
# Formula:
#     reward = stage_advanced
#            − llm_cost_cents * llm_cost_weight
#            − k * (retracted_claims + unsubscribes + complaints)
#            + terminal_multiplier  (when Stripe payment_intent.succeeded
#                                    is attributable to this opportunity)
#
# Coefficients are env-tunable so a deployment can soft-shift weights
# without a code change; defaults match the spec.

STAGE_WEIGHT_ENV = "SAMUS_REWARD_STAGE_WEIGHT"
LLM_COST_WEIGHT_ENV = "SAMUS_REWARD_LLM_COST_WEIGHT"
HARM_K_ENV = "SAMUS_REWARD_HARM_K"
TERMINAL_MULTIPLIER_ENV = "SAMUS_REWARD_TERMINAL_MULTIPLIER"

DEFAULT_STAGE_WEIGHT: float = 1.0
DEFAULT_LLM_COST_WEIGHT: float = 0.01
DEFAULT_HARM_K: float = 5.0
DEFAULT_TERMINAL_MULTIPLIER: float = 100.0

# CRM Opportunity stages, in pipeline order. ``stage_advanced`` is the count
# of advances FROM "new" along this ordering, with both terminal-won variants
# collapsing to the same final index so a retainer-close doesn't out-score a
# straight win.
STAGE_ORDER: tuple[str, ...] = (
    "new",
    "qualified",
    "proposal",
    "negotiation",
    "closed_won",
)
_TERMINAL_WON_STAGES = frozenset({"closed_won", "closed_won_retainer"})
_TERMINAL_STAGES = frozenset({"closed_won", "closed_won_retainer", "closed_lost"})

_DEFAULT_PERSIST_PATH = "/opt/samus/data/host_artifacts/reward_computations.jsonl"
_PERSIST_PATH_ENV = "SAMUS_REWARD_PERSIST_PATH"


class RewardPersistenceError(RuntimeError):
    """Raised when the reward audit ledger write fails.

    Fail-CLOSED: a reward we can't trace is a reward we don't return. ADR-004
    requires every reward computation be appended to the audit ledger before
    the bandit consumes it.
    """


@dataclass
class RewardComputation:
    """One ADR-004 reward computation with its per-term breakdown.

    ``components`` carries every named term so debugging never has to
    re-derive the math from raw inputs. Persistence captures both ``reward``
    and ``components`` so an operator can reconstruct any historical
    decision from the ledger alone.
    """

    reward: float
    components: dict[str, float]
    opportunity_id: str
    computed_at: datetime
    correlation_id: str = ""


class RewardComputationStore(Protocol):
    """Read-side interface for the four inputs to :func:`compute_reward`.

    Production callers pass nothing; the default store wraps the CRM +
    finance workcells. Tests pass a stub.
    """

    def opportunity(self, opportunity_id: str) -> dict[str, Any] | None: ...

    def llm_cost_cents(self, opportunity_id: str) -> int: ...

    def stripe_payment_succeeded(self, opportunity_id: str) -> bool: ...

    # Harm collectors share the harm_signals.HarmSignalStore protocol; the
    # default implementation forwards to it.
    def harm_signal_store(self) -> Any: ...


class DefaultRewardComputationStore:
    """Production store — pulls from CRM Opportunity, the finance event log,
    and the harm_signals collectors.

    LLM cost is read from the Opportunity row's ``token_cost_usd`` field
    (the per-prospect dollar spend during discovery; see
    :mod:`backend.crm.models`.``Opportunity.token_cost_usd``). The figure is
    converted to cents at read-time so the formula's "cost in cents" stays
    dimensionally honest.

    Stripe attribution: a ``payment_intent.succeeded`` event whose
    ``client_reference_id`` matches ``op_<opportunity_id>`` is the
    canonical terminal signal (see :mod:`backend.finance.webhook`). Phase-1
    reads via :func:`backend.finance.webhook.load_recent_events` over a
    90-day window — the same source the morning briefing uses.
    """

    _STRIPE_LOOKBACK_DAYS = 90

    def opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        from backend.crm import service as crm_service

        opp = crm_service.get_opportunity(opportunity_id)
        return opp.model_dump() if opp is not None else None

    def llm_cost_cents(self, opportunity_id: str) -> int:
        opp = self.opportunity(opportunity_id)
        if opp is None:
            return 0
        usd = opp.get("token_cost_usd") or 0.0
        try:
            return max(0, int(round(float(usd) * 100)))
        except (TypeError, ValueError):
            return 0

    def stripe_payment_succeeded(self, opportunity_id: str) -> bool:
        from backend.finance import webhook as finance_webhook

        marker = f"op_{opportunity_id}"
        try:
            events = finance_webhook.load_recent_events(self._STRIPE_LOOKBACK_DAYS)
        except Exception as exc:  # noqa: BLE001 — fail-OPEN at the store
            _LOG.warning(
                "stripe event scan failed opp=%s: %s",
                opportunity_id,
                exc,
            )
            return False
        for ev in events:
            event_type = getattr(ev, "event_type", "") or ""
            cref = getattr(ev, "client_reference_id", "") or ""
            if event_type == "payment_intent.succeeded" and cref == marker:
                return True
            # checkout.session.completed for payment mode is the same
            # terminal economic event — Stripe fires both for a hosted
            # checkout buy-link flow. Treat either as the multiplier
            # trigger so the multiplier never depends on which webhook
            # arrived first.
            if (
                event_type == "checkout.session.completed"
                and cref == marker
                and (getattr(ev, "payment_status", "") or "") == "paid"
            ):
                return True
        return False

    def harm_signal_store(self) -> Any:
        from backend.strategy.harm_signals import DefaultHarmSignalStore

        return DefaultHarmSignalStore()


def _coef_from_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        _LOG.warning(
            "ignoring malformed %s=%r; using default %s",
            name,
            raw,
            default,
        )
        return default


def _stage_advanced(opportunity: dict[str, Any]) -> int:
    """Count stage advances from "new" along STAGE_ORDER.

    closed_won_retainer collapses to the closed_won index so a retainer
    close doesn't out-score a straight win. closed_lost contributes the
    advance count up to (but not including) the terminal — the deal moved
    forward before it died, and ADR-004's intent is that advancement is
    rewarded regardless of terminal outcome (the terminal multiplier is
    a separate, payment-gated term).
    """
    stage = (opportunity.get("stage") or "new").strip()
    if stage in _TERMINAL_WON_STAGES:
        return len(STAGE_ORDER) - 1  # new -> ... -> closed_won
    if stage == "closed_lost":
        # Lost deals get credit for whatever stage they reached before the
        # close; on a row with no prior history we conservatively credit 0.
        # Phase-1 reads ``stage`` only — a future iteration can replay the
        # Opportunity's audit trail for a per-advance fine-grain score.
        return 0
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def _persist_path() -> Path:
    return Path(os.getenv(_PERSIST_PATH_ENV, _DEFAULT_PERSIST_PATH))


def _append_to_ledger(comp: RewardComputation) -> None:
    """Append one RewardComputation row to the audit JSONL.

    Raises :class:`RewardPersistenceError` on any failure so the caller
    can fail CLOSED. The directory is created on demand because the
    default path lives under ``/opt/samus/data/host_artifacts`` which a
    fresh checkout will not have.
    """
    path = _persist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RewardPersistenceError(
            f"reward ledger mkdir failed at {path.parent}: {exc}",
        ) from exc
    row = {
        "opportunity_id": comp.opportunity_id,
        "reward": comp.reward,
        "components": comp.components,
        "computed_at": comp.computed_at.isoformat(),
        "correlation_id": comp.correlation_id,
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise RewardPersistenceError(
            f"reward ledger append failed at {path}: {exc}",
        ) from exc


def _build_proposed_action(comp: RewardComputation) -> Any:
    """Build the ProposedAction passed through the Codex check.

    Imports are deferred so the codex layer is loaded only when a reward
    actually computes (keeps the reward_density module importable in
    test/CI processes where the codex registry may not yet be primed).
    """
    from backend.common.codex import ProposedAction

    return ProposedAction(
        service="strategy",
        capability="reward_density",
        action_kind="reward_function_update",
        payload={
            "opportunity_id": comp.opportunity_id,
            "reward": comp.reward,
            "components": comp.components,
            "subtracts_harm": True,
        },
        proposed_by="strategy.reward_density.compute_reward",
        correlation_id=comp.correlation_id or None,
    )


def compute_reward(
    opportunity_id: str,
    *,
    store: RewardComputationStore | None = None,
    correlation_id: str = "",
) -> RewardComputation:
    """Compute the ADR-004 reward for one Opportunity.

    Pulls stage history (from the CRM Opportunity row), LLM cost
    (per-prospect dollar spend captured at Opportunity creation), the
    three harm signals (retracted claims, unsubscribes, SES complaints),
    and the Stripe terminal signal. Returns a :class:`RewardComputation`
    with a per-term ``components`` breakdown.

    Fail-CLOSED behaviour:
      * All three harm collectors erroring -> raises (the safety failure).
      * Audit-ledger write failure -> raises :class:`RewardPersistenceError`.
      * Codex registry unavailable -> :class:`CodexUnavailable` propagates.
      * Codex returns a blocking verdict -> raises :class:`CodexViolation`.
    """
    s = store if store is not None else DefaultRewardComputationStore()
    opp = s.opportunity(opportunity_id)
    if opp is None:
        raise ValueError(f"opportunity not found: {opportunity_id!r}")

    stage_weight = _coef_from_env(STAGE_WEIGHT_ENV, DEFAULT_STAGE_WEIGHT)
    cost_weight = _coef_from_env(LLM_COST_WEIGHT_ENV, DEFAULT_LLM_COST_WEIGHT)
    harm_k = _coef_from_env(HARM_K_ENV, DEFAULT_HARM_K)
    terminal_mult = _coef_from_env(
        TERMINAL_MULTIPLIER_ENV,
        DEFAULT_TERMINAL_MULTIPLIER,
    )

    stage_advanced = _stage_advanced(opp)
    llm_cost_cents = max(0, int(s.llm_cost_cents(opportunity_id)))

    harm_store = s.harm_signal_store()
    from backend.strategy import harm_signals as _harm

    # Fail-CLOSED if every harm read fails simultaneously — that pattern is
    # the safety failure the audit trail cannot quietly absorb. Individual
    # collector failures fail-OPEN (return 0) by contract.
    harm_errors: list[BaseException] = []
    retracted = 0
    unsubs = 0
    complaints = 0
    try:
        retracted = _harm.retracted_claims_for(opportunity_id, store=harm_store)
    except BaseException as exc:  # noqa: BLE001 — collected for the closure check
        harm_errors.append(exc)
    try:
        unsubs = _harm.unsubscribes_for(opportunity_id, store=harm_store)
    except BaseException as exc:  # noqa: BLE001
        harm_errors.append(exc)
    try:
        complaints = _harm.complaints_for(opportunity_id, store=harm_store)
    except BaseException as exc:  # noqa: BLE001
        harm_errors.append(exc)
    if len(harm_errors) >= 3:
        raise RuntimeError(
            "all three harm collectors failed; refusing to score "
            f"opportunity_id={opportunity_id!r}",
        ) from harm_errors[0]

    harm_count = retracted + unsubs + complaints
    terminal_paid = bool(s.stripe_payment_succeeded(opportunity_id))

    stage_term = float(stage_advanced) * stage_weight
    cost_term = float(llm_cost_cents) * cost_weight
    harm_term = float(harm_count) * harm_k
    terminal_term = terminal_mult if terminal_paid else 0.0

    raw_reward = stage_term - cost_term - harm_term + terminal_term
    # G7 spec: negative reward clips to zero.
    reward = raw_reward if raw_reward > 0.0 else 0.0

    components: dict[str, float] = {
        "stage_advanced": float(stage_advanced),
        "stage_term": stage_term,
        "llm_cost_cents": float(llm_cost_cents),
        "llm_cost_term": cost_term,
        "retracted_claims": float(retracted),
        "unsubscribes": float(unsubs),
        "complaints": float(complaints),
        "harm_count": float(harm_count),
        "harm_term": harm_term,
        "terminal_paid": 1.0 if terminal_paid else 0.0,
        "terminal_term": terminal_term,
        "raw_reward": raw_reward,
        "clipped_to_zero": 1.0 if raw_reward <= 0.0 else 0.0,
        # Coefficients in effect at compute time — operators tuning env
        # vars can replay any historical row knowing exactly what weights
        # produced it.
        "stage_weight": stage_weight,
        "llm_cost_weight": cost_weight,
        "harm_k": harm_k,
        "terminal_multiplier": terminal_mult,
    }

    comp = RewardComputation(
        reward=reward,
        components=components,
        opportunity_id=opportunity_id,
        computed_at=datetime.now(timezone.utc),
        correlation_id=correlation_id,
    )

    # Audit-ledger append BEFORE the codex check so a reward that the codex
    # later rejects is still traceable in the operator's audit trail.
    # Persistence failure is itself fail-CLOSED (RewardPersistenceError).
    _append_to_ledger(comp)

    # Codex gate — VW-G7 must not fire (subtracts_harm=True). A blocking
    # verdict (e.g. banned-phrase contamination of the payload, future
    # blocking flip of VW-G7 -> VR-G7) raises CodexViolation; the caller
    # decides how to surface that — we never silently swallow it.
    from backend.common.codex import CodexViolation, check_action

    action = _build_proposed_action(comp)
    verdict = check_action(action)
    if not verdict.allowed:
        raise CodexViolation(
            verdict.violated_rule_id or "VR-UNKNOWN",
            verdict.reason or "codex blocked reward_function_update",
            drafted_adr_path=(Path(verdict.drafted_adr_path) if verdict.drafted_adr_path else None),
        )

    return comp
