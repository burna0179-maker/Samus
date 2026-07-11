"""Hedge-fund-style portfolio decision layer.

Ported from recovery/llm_portfolio_manager.py.

Tier-3 note: the multi-armed bandit is UCB1; the RL feedback loop is a simple
credit-assignment dict. Real RL training is still a deferral — it requires
live CRM conversion signals at a volume that does not yet exist.

Bandit persistence (strategy-integration build, Unit 1): the bandit's
``_BANDIT_STATS`` / ``_BANDIT_TOTAL`` are no longer in-memory only. They are
backed by :mod:`backend.strategy.bandit_store` — a DynamoDB table
(``samus_strategy_bandit``, atomic ``ADD`` counters) with a JSON-file
fallback. This makes the **decide** step (host-side 07:30 prospecting) and
the **learn** step (container-side CRM/strategy) — separate processes,
separate memory — share one durable bandit, and survives restarts. The
pure-logic API below (``update_bandit``, ``select_best_policy``,
``get_bandit_stats``, ``_ucb1_score``, ``reset_bandit`` …) is unchanged;
only the storage moved. ``_BANDIT_STATS`` survives as a short-TTL read cache,
not the source of truth.

Three-layer stack (canonical analogue §6 model_extended + agents plane):

  Layer 1 — THINKS: ``propose_allocation()``  — LLM macro reasoning
  Layer 2 — LEARNS: ``update_bandit()``        — UCB1 multi-armed bandit
  Layer 3 — ACTS:   optimizer.rank_portfolio() — rule-based ranker (sibling module)

LLM calls route through ``backend.common.llm_client.anthropic_messages`` with
``workcell="strategy"`` so every token spend is metered by the budget gate.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # import only for type hints — no runtime cost / circular risk
    from .reward_density import RewardSignal

__all__ = [
    "PortfolioState",
    "AllocationDecision",
    "propose_allocation",
    "update_bandit",
    "record_outcome",
    "get_bandit_stats",
    "reset_bandit",
    "POLICY_FAMILIES",  # noqa: F822
    "update_policy_bandit",
    "select_best_policy",
    "get_policy_bandit_stats",
    "HIERARCHICAL_ARM_SEP",
    "CHANNELS",
    "CHANNEL_ARM_NAMESPACE",
    "update_channel_bandit",
    "select_best_channel",
    "get_channel_bandit_stats",
]

_LOG = logging.getLogger("samus.strategy.portfolio_manager")


# ---------------------------------------------------------------------------
# Pydantic-free data structures (dataclasses at I/O boundary; pure internal)
# ---------------------------------------------------------------------------


@dataclass
class PortfolioState:
    """Snapshot of the current pipeline state passed to the LLM.

    Attributes mirror ``recovery/llm_portfolio_manager.py::PortfolioState``
    verbatim so future calibration work can reference the original directly.
    """

    budget_remaining: float
    avg_conversion_rate: float
    pipeline_value: float
    active_prospects: int
    prospects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AllocationDecision:
    """Output of ``propose_allocation``.

    ``priorities``    — prospect_ids the LLM recommends accelerating.
    ``deprioritize``  — prospect_ids to defer or drop.
    ``actions``       — list of ``{"type": str, "prospect_id": str}`` dicts.
    ``raw_text``      — unparsed LLM response for audit logging.
    ``parse_error``   — True when the LLM returned non-JSON (fail-closed path).
    """

    priorities: list[str] = field(default_factory=list)
    deprioritize: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    raw_text: str = ""
    parse_error: bool = False


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

# Static system prompt — kept verbatim and unchanging so it stays
# prompt-cache-friendly (Control D). All per-call variability goes in the
# user template, never here.
_PORTFOLIO_MANAGER_SYSTEM = (
    "You are a hedge fund portfolio manager optimising a pipeline of B2B "
    "service deals. Maximise REWARD DENSITY, not raw conversion count: a "
    "deal is worth more when it carries durable signals — deep enrichment "
    "(owner email, social handles), healthy infrastructure, a wide SEO "
    "opportunity gap, high conversion probability, low token cost and fast "
    "resolution latency. Prefer verticals with persistent positive momentum "
    "and rising EMA trend; avoid volatile transient spikes and "
    "shallow-enrichment sectors that will not compound. Budget is limited; "
    "execution is expensive; time decay reduces conversion probability. "
    "Return strict JSON only — no prose, no markdown fences."
)

_PORTFOLIO_MANAGER_TEMPLATE = """\
CURRENT PORTFOLIO STATE:
{state_json}

PREDICTIVE MARKET SIGNALS (industry reward-density / momentum / token-efficiency / regret / EMA velocity):
{signals_json}

INSTRUCTIONS:
1. Identify the top opportunities with highest reward density — weigh
   enrichment depth, infrastructure health, SEO gap, conversion probability,
   token efficiency and low latency together, not conversion alone.
2. Identify underperforming assets (high cost, low probability, stale stage,
   shallow enrichment, or sitting in a saturated/declining vertical).
3. Favour verticals whose momentum and EMA trend are persistently positive;
   discount transient spikes.
4. Recommend capital allocation and actions.

Return JSON matching exactly this schema:
{{
  "priorities": [<prospect_id>, ...],
  "deprioritize": [<prospect_id>, ...],
  "actions": [
    {{"type": "accelerate"|"replan"|"drop", "prospect_id": "<id>"}}
  ]
}}
"""


def _build_prompt(state: PortfolioState, market_signals: dict[str, Any]) -> str:
    state_json = json.dumps(
        {
            "budget_remaining": state.budget_remaining,
            "avg_conversion_rate": state.avg_conversion_rate,
            "pipeline_value": state.pipeline_value,
            "active_prospects": state.active_prospects,
            "prospects": state.prospects,
        },
        default=str,
        indent=2,
    )
    # market_signals carries the compact predictive payload (industry
    # reward-density / momentum / token-efficiency / regret / ema-velocity
    # summaries). Empty dict degrades gracefully to an explicit "none".
    signals_json = json.dumps(market_signals or {}, default=str, indent=2)
    return _PORTFOLIO_MANAGER_TEMPLATE.format(
        state_json=state_json,
        signals_json=signals_json,
    )


def _parse_llm_response(raw: str) -> AllocationDecision:
    """Parse LLM JSON into AllocationDecision. Fail-closed on bad JSON."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        _LOG.warning("portfolio_manager: LLM returned non-JSON; fail-closed")
        return AllocationDecision(raw_text=raw, parse_error=True)
    return AllocationDecision(
        priorities=obj.get("priorities") or [],
        deprioritize=obj.get("deprioritize") or [],
        actions=obj.get("actions") or [],
        raw_text=raw,
        parse_error=False,
    )


def propose_allocation(
    portfolio_state: PortfolioState,
    market_signals: dict[str, Any],
    *,
    api_key: str | None = None,
) -> AllocationDecision:
    """Call the LLM to produce a high-level allocation recommendation.

    Uses ``backend.common.llm_client.anthropic_messages`` with
    ``workcell="strategy"`` so spend is metered by the budget gate.

    If the API key is absent or the budget gate denies the call the function
    degrades gracefully: it logs a warning and returns an empty
    ``AllocationDecision`` with ``parse_error=True`` rather than raising.

    ``market_signals`` is the compact predictive payload — industry
    reward-density / momentum / token-efficiency / regret / ema-velocity
    summaries — and is wired directly into the prompt context. An empty dict
    degrades gracefully (the prompt renders an explicit empty payload).

    Exactly ONE ``anthropic_messages`` call is made regardless of inputs.
    """
    from backend.common.llm_client import (  # local import — avoids circular at module load
        BudgetExceeded,
        LlmCallError,
        anthropic_messages,
    )

    prompt = _build_prompt(portfolio_state, market_signals)

    try:
        raw_text, usage = anthropic_messages(
            workcell="strategy",
            api_key="unused",
            prompt=prompt,
            system=_PORTFOLIO_MANAGER_SYSTEM,
            max_tokens=768,
        )
    except BudgetExceeded as exc:
        _LOG.info("propose_allocation: budget gate denied — %s", exc)
        return AllocationDecision(parse_error=True)
    except LlmCallError as exc:
        _LOG.warning("propose_allocation: LLM call failed — %s", exc)
        return AllocationDecision(parse_error=True)

    _LOG.debug(
        "propose_allocation: tokens in=%d out=%d",
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )
    return _parse_llm_response(raw_text)


# ---------------------------------------------------------------------------
# UCB1 multi-armed bandit — DDB-durable (strategy-integration build, Unit 1)
# ---------------------------------------------------------------------------
# State shape (unchanged): dict[arm_id, {"wins": float, "trials": int}].
#
# The source of truth is :mod:`backend.strategy.bandit_store` — a DynamoDB
# table with a JSON-file fallback. ``_BANDIT_STATS`` / ``_BANDIT_TOTAL`` below
# are NOT the state any more; they are a short-lived read cache, repopulated
# from the store on every read path. This is what lets the host-side decide
# step and the container-side learn step (separate processes) share one
# bandit. Reset via ``reset_bandit()`` in tests.
#
# ``_BANDIT_TOTAL`` is derived as ``sum(trials)`` over all arms — it is never
# persisted as its own row (see bandit_store.py module docstring for why).

# UCB1 exploration bias. 2.0 is the textbook constant (Auer et al. 2002).
_UCB1_DEFAULT_BIAS: float = 2.0

# Local read cache. Mirror of the most recent store snapshot; the store is
# authoritative. ``_BANDIT_TOTAL`` is the cached sum-of-trials for that
# snapshot. Both are refreshed by ``_load_bandit_state``.
_BANDIT_STATS: dict[str, dict[str, float]] = {}
_BANDIT_TOTAL: int = 0


def _bandit_store() -> Any:
    """Return the process-wide durable bandit store.

    Local import — keeps ``portfolio_manager`` loadable (and the pure-logic
    functions unit-testable) without a hard dependency on boto3 / the store
    module at import time.
    """
    from .bandit_store import get_default_store

    return get_default_store()


def _load_bandit_state() -> tuple[dict[str, dict[str, float]], int]:
    """Read the authoritative bandit state from the store into the cache.

    Returns ``(_BANDIT_STATS, _BANDIT_TOTAL)`` after refreshing both from the
    store. ``_BANDIT_TOTAL`` is derived here as the sum of every arm's
    ``trials``. ``BanditStore`` already swallows backend faults; this extra
    guard covers a catastrophic failure of the store object itself (or its
    accessor) so the bandit degrades to an empty state rather than raising
    out of ``get_bandit_stats`` / ``select_best_policy`` — the same
    allow-on-persistence-failure posture as ``llm_budget``.
    """
    global _BANDIT_TOTAL
    try:
        arms = _bandit_store().read_all()
        stats: dict[str, dict[str, float]] = {
            arm_id: {"wins": float(arm.wins), "trials": int(arm.trials)}
            for arm_id, arm in arms.items()
        }
    except Exception as exc:  # noqa: BLE001 - never raise out of a read path
        _LOG.warning("portfolio_manager: bandit store read failed (%s); empty", exc)
        stats = {}
    total = sum(int(row["trials"]) for row in stats.values())
    _BANDIT_STATS.clear()
    _BANDIT_STATS.update(stats)
    _BANDIT_TOTAL = total
    return _BANDIT_STATS, _BANDIT_TOTAL


def _ucb1_score(
    wins: float,
    trials: int,
    total: int,
    bias: float = _UCB1_DEFAULT_BIAS,
) -> float:
    """UCB1: mean + sqrt(bias * ln(total) / trials).

    Pure function — no I/O, no storage. Unchanged from the in-memory bandit.
    """
    if trials == 0:
        return math.inf
    mean = wins / trials
    if total <= 0:
        return mean
    return mean + math.sqrt(bias * math.log(total) / trials)


def update_bandit(
    arm_id: str,
    outcome: float,
    *,
    reward_signal: "RewardSignal | None" = None,
) -> None:
    """Record one trial outcome for ``arm_id``.

    ``outcome`` is a reward scalar (e.g. 1.0 for a conversion, 0.0 for no
    response). The bandit uses this to update its UCB1 statistics.

    When ``reward_signal`` is provided the recorded reward becomes
    ``compute_reward_density(reward_signal)`` — the reward-density model —
    instead of the raw ``outcome`` scalar. When ``reward_signal`` is None the
    behaviour is byte-identical to the legacy scalar path (existing callers
    and the ``update_bandit_arm`` app action are unaffected).

    Storage: the arm's row is persisted to the durable bandit store via an
    atomic ``ADD`` (DynamoDB) or read-modify-write (JSON fallback). The atomic
    ADD is what keeps a host-side decide writer and a container-side learn
    writer from clobbering each other. Persistence failure never raises — the
    update degrades to cache-only (mirrors ``llm_budget`` allow-on-failure).
    """
    reward = float(outcome)
    if reward_signal is not None:
        # Local import — keeps portfolio_manager loadable even if a future
        # reward_density refactor introduces a heavy dependency.
        from .reward_density import compute_reward_density

        reward = float(compute_reward_density(reward_signal))
    # Persist first (store is the source of truth); one trial == +1.
    # The store swallows backend faults; this guard additionally covers a
    # catastrophic failure of the store object / accessor so update_bandit
    # never raises (allow-on-persistence-failure, mirrors llm_budget).
    persisted = False
    try:
        persisted = bool(_bandit_store().add(arm_id, wins_delta=reward, trials_delta=1))
    except Exception as exc:  # noqa: BLE001 - never raise out of a write path
        _LOG.warning("update_bandit: bandit store add failed (%s); arm=%s", exc, arm_id)
    # Refresh the read cache so a subsequent get_bandit_stats() in the same
    # process sees this write immediately (read-your-own-writes).
    _load_bandit_state()
    entry = _BANDIT_STATS.get(arm_id, {"wins": 0.0, "trials": 0})
    _LOG.debug(
        "update_bandit: arm=%s wins=%.3f trials=%d total=%d density=%s",
        arm_id,
        float(entry.get("wins", 0.0)),
        int(entry.get("trials", 0)),
        _BANDIT_TOTAL,
        reward_signal is not None,
    )
    # Learning telemetry: make WHAT the bandit learned reconstructable -- the
    # arm, the outcome it credited, its resulting wins/trials, and (crucially)
    # whether the durable store PERSISTED the write or silently degraded to
    # cache-only. Additive; guarded so a telemetry fault can never raise out of
    # this write path (mirrors the store's allow-on-failure contract).
    try:
        from backend.common import learning_telemetry as _learning_telemetry

        _learning_telemetry.record_learning_update(
            kind=_learning_telemetry.KIND_BANDIT,
            arm_id=arm_id,
            outcome=float(outcome),
            reward=reward,
            wins=float(entry.get("wins", 0.0)),
            trials=int(entry.get("trials", 0)),
            persisted=persisted,
            density_applied=reward_signal is not None,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the write path
        _LOG.debug("update_bandit learning telemetry skipped: %s", exc)


def get_bandit_stats() -> dict[str, dict[str, float]]:
    """Return a snapshot copy of current bandit statistics from the store."""
    stats, _total = _load_bandit_state()
    return {k: dict(v) for k, v in stats.items()}


def reset_bandit() -> None:
    """Clear all bandit state. For test isolation only.

    Clears the in-memory cache AND the durable store's JSON fallback file
    (the test-suite store-of-truth). It intentionally does NOT wipe real
    DynamoDB — production bandit history must survive a test run; in the test
    suite ``DDB_STRATEGY_BANDIT_TABLE=""`` so the JSON file IS the store and
    this fully resets state.

    Also clears the hierarchical ``industry::policy_family`` arms — they live
    in the same store / ``_BANDIT_STATS`` dict, so one clear covers both the
    flat and the hierarchical bandit.
    """
    global _BANDIT_TOTAL
    try:
        _bandit_store().clear()
    except Exception as exc:  # noqa: BLE001 - reset is best-effort
        _LOG.warning("reset_bandit: bandit store clear failed (%s)", exc)
    _BANDIT_STATS.clear()
    _BANDIT_TOTAL = 0


# ---------------------------------------------------------------------------
# Hierarchical bandit — industry → policy family (2-level)
# ---------------------------------------------------------------------------
# A vertical is heterogeneous internally: different execution styles carry
# different reward density. Selecting *policy families within a vertical*
# (rather than raw industries) converges faster and cuts exploration regret.
#
# Composite arm keys ``f"{industry}::{policy_family}"`` are stored in the SAME
# ``_BANDIT_STATS`` dict as the flat arms — no parallel structure. The flat
# ``update_bandit`` / ``get_bandit_stats`` / ``reset_bandit`` paths are
# unaffected and stay byte-identical.
#
# The policy-family names are NOT defined here: ``policy_compiler`` owns them
# via ``VERTICAL_POLICIES``. ``POLICY_FAMILIES`` below is an import alias so
# there is exactly one source of truth.

HIERARCHICAL_ARM_SEP: str = "::"


def _composite_arm(industry: str, policy_family: str) -> str:
    """Compose the ``industry::policy_family`` hierarchical arm key."""
    return f"{industry}{HIERARCHICAL_ARM_SEP}{policy_family}"


def _policy_families() -> dict[str, tuple[str, ...]]:
    """Return the per-vertical policy-family map owned by ``policy_compiler``.

    Local import — keeps ``portfolio_manager`` loadable independently and
    avoids any import cycle at module load.
    """
    from .policy_compiler import POLICY_FAMILIES as _PF

    return _PF


def update_policy_bandit(
    industry: str,
    policy_family: str,
    outcome: float,
    *,
    reward_signal: "RewardSignal | None" = None,
) -> None:
    """Record one trial for a hierarchical ``industry::policy_family`` arm.

    Composes the composite key and delegates to :func:`update_bandit`, so the
    reward-density weighting (``reward_signal``) flows through unchanged. The
    composite arm is stored alongside the flat arms in ``_BANDIT_STATS``.
    """
    update_bandit(
        _composite_arm(industry, policy_family),
        outcome,
        reward_signal=reward_signal,
    )


def select_best_policy(industry: str) -> str:
    """UCB1-select the best policy family for ``industry``.

    Scores each of the industry's policy-family arms with :func:`_ucb1_score`
    (reusing the flat bandit's scorer). An unseen family has ``trials == 0`` so
    ``_ucb1_score`` returns ``inf`` — every family is therefore explored once
    before exploitation begins. Returns the chosen ``policy_family`` (not the
    composite key). An unknown industry falls back to the generic family set.
    """
    families = _policy_families().get(industry)
    if not families:
        from .policy_compiler import GENERIC_VERTICAL_POLICY

        families = GENERIC_VERTICAL_POLICY.policy_families

    # Read the authoritative state from the durable store before scoring —
    # the decide step runs host-side and must see learn-step writes that
    # happened in the container process.
    stats, total = _load_bandit_state()
    best_family = families[0]
    best_score = -math.inf
    for family in families:
        entry = stats.get(_composite_arm(industry, family))
        wins = float(entry["wins"]) if entry else 0.0
        trials = int(entry["trials"]) if entry else 0
        score = _ucb1_score(wins, trials, total)
        if score > best_score:
            best_score = score
            best_family = family
    return best_family


def get_policy_bandit_stats(industry: str) -> dict[str, dict[str, float]]:
    """Return a snapshot of the composite arms for one ``industry`` only.

    Keys are the composite ``industry::policy_family`` strings; flat arms and
    other industries' arms are excluded.
    """
    prefix = f"{industry}{HIERARCHICAL_ARM_SEP}"
    all_stats, _total = _load_bandit_state()
    return {
        arm_id: dict(arm_stats)
        for arm_id, arm_stats in all_stats.items()
        if arm_id.startswith(prefix)
    }


# Alias — ``policy_compiler`` owns the family names; this re-export lets
# callers reach them through either module without a second definition.
def __getattr__(name: str) -> object:  # pragma: no cover - thin import alias
    """Lazily expose ``POLICY_FAMILIES`` from ``policy_compiler``.

    Module-level ``__getattr__`` keeps the family map a single source of truth
    while still allowing ``portfolio_manager.POLICY_FAMILIES`` access.
    """
    if name == "POLICY_FAMILIES":
        return _policy_families()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Channel bandit — call / email / seo / retention (HOTL Tranche 2)
# ---------------------------------------------------------------------------
# A parallel arm DIMENSION alongside the hierarchical ``industry::family``
# arms: which *work type* pays, independent of vertical. This is the economic
# arbiter's "should I call or email?" answer. Same storage, same UCB1, no
# schema change — the arm id is just namespaced ``channel::<name>`` so it
# can never collide with an industry named like a channel.

CHANNEL_ARM_NAMESPACE: str = "channel"

# The four work types the arbiter allocates between. A fixed tuple (not
# operator config) — reward plumbing for a new channel is code work anyway.
CHANNELS: tuple[str, ...] = ("call", "email", "seo", "retention")


def _channel_arm(channel: str) -> str:
    """Compose the ``channel::<name>`` namespaced arm key."""
    return f"{CHANNEL_ARM_NAMESPACE}{HIERARCHICAL_ARM_SEP}{channel}"


def update_channel_bandit(
    channel: str,
    reward: float,
    *,
    reward_signal: "RewardSignal | None" = None,
) -> None:
    """Record one trial outcome for a channel arm.

    Unknown channel names are recorded too (forward-compatible with a future
    channel) — selection only ever ranks the canonical :data:`CHANNELS`.
    Delegates to :func:`update_bandit`, so durability + reward-density
    weighting behave identically to every other arm.
    """
    update_bandit(_channel_arm(channel), reward, reward_signal=reward_signal)


def select_best_channel(channels: tuple[str, ...] | None = None) -> str:
    """UCB1-select the best work channel.

    Each unseen channel scores ``inf`` so every channel is explored once
    before exploitation begins (same behaviour as ``select_best_policy``).
    ``channels`` narrows the candidate set (e.g. exclude ``call`` outside
    business hours); defaults to all of :data:`CHANNELS`.
    """
    candidates = channels or CHANNELS
    stats, total = _load_bandit_state()
    best = candidates[0]
    best_score = -math.inf
    for channel in candidates:
        entry = stats.get(_channel_arm(channel))
        wins = float(entry["wins"]) if entry else 0.0
        trials = int(entry["trials"]) if entry else 0
        score = _ucb1_score(wins, trials, total)
        if score > best_score:
            best_score = score
            best = channel
    return best


def get_channel_bandit_stats() -> dict[str, dict[str, float]]:
    """Snapshot of the ``channel::*`` arms only, keyed by bare channel name."""
    prefix = f"{CHANNEL_ARM_NAMESPACE}{HIERARCHICAL_ARM_SEP}"
    all_stats, _total = _load_bandit_state()
    return {
        arm_id[len(prefix) :]: dict(arm_stats)
        for arm_id, arm_stats in all_stats.items()
        if arm_id.startswith(prefix)
    }


# ---------------------------------------------------------------------------
# RL feedback loop — simple credit-assignment dict (tier-3 deferred)
# ---------------------------------------------------------------------------
# Shape: dict[prospect_id, {"action": str, "result": str, "credit": float}]
# Real RL training (Q-learning / policy gradient) deferred until signal volume
# is sufficient to avoid overfitting. This skeleton captures outcome signals
# so the training dataset can be reconstructed later.

_RL_CREDITS: dict[str, dict[str, Any]] = {}

# ADR-004 harm-aware bandit reward — DORMANT by default. When
# SAMUS_REWARD_ADR004_BANDIT_ENABLED is truthy, record_outcome credits the
# bandit arm with backend.strategy.reward_density.compute_reward — the ADR-004
# opportunity-level reward (stage_advanced − llm_cost − k·harm + stripe
# terminal, codex-gated by VR-G7) — instead of the flat won/lost scalar. This
# is the "wire the reward function into the bandit" step, kept deactivated
# until the operator flips the flag; off → byte-identical legacy behaviour.
_ADR004_BANDIT_FLAG = "SAMUS_REWARD_ADR004_BANDIT_ENABLED"


def _adr004_bandit_enabled() -> bool:
    return os.getenv(_ADR004_BANDIT_FLAG, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _adr004_reward_for_prospect(prospect_id: str) -> float | None:
    """ADR-004 reward for the prospect's opportunity, or None on any failure.

    Fail-OPEN: a missing opportunity / CRM outage / codex block / ledger
    failure returns None so the caller falls back to the scalar credit and the
    learn path never breaks. Imports are deferred so this module stays loadable
    when CRM / the codex registry aren't primed (tests, CLI).
    """
    try:
        from backend.crm import service as crm_service

        opp = crm_service.get_opportunity_for_prospect(prospect_id)
        if opp is None:
            return None
        from .reward_density import compute_reward

        return float(compute_reward(opp.opportunity_id).reward)
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        _LOG.warning(
            "ADR-004 bandit reward unavailable for prospect=%s: %s",
            prospect_id,
            exc,
        )
        return None


def record_outcome(prospect_id: str, action: str, result: str) -> None:
    """Record an action-outcome pair for future RL credit assignment.

    ``result`` should be one of ``"won"``, ``"lost"``, ``"stale"``.
    Credit assignment formula (seed values; tune with data):
      won   → +1.0
      lost  → -0.5
      stale → -0.1
      other → 0.0

    The credit dict is in-memory only. When real RL training is added this
    dict feeds the training buffer.
    """
    credit_map = {"won": 1.0, "lost": -0.5, "stale": -0.1}
    credit = credit_map.get(result, 0.0)
    _RL_CREDITS[prospect_id] = {
        "action": action,
        "result": result,
        "credit": credit,
    }
    _LOG.debug(
        "record_outcome: prospect=%s action=%s result=%s credit=%.2f",
        prospect_id,
        action,
        result,
        credit,
    )
    # Propagate as bandit feedback: the action name is the arm_id. When the
    # ADR-004 harm-aware reward is ACTIVATED (default OFF), credit the arm with
    # the full opportunity-level reward instead of the flat scalar; otherwise
    # keep the legacy scalar. Fail-open: an unavailable ADR-004 reward (None)
    # falls back to the scalar so the learn path never breaks.
    bandit_reward = max(credit, 0.0)
    if _adr004_bandit_enabled():
        adr004 = _adr004_reward_for_prospect(prospect_id)
        if adr004 is not None:
            bandit_reward = adr004
    update_bandit(action, bandit_reward)


def get_rl_credits() -> dict[str, dict[str, Any]]:
    """Return a snapshot copy of all recorded RL credits."""
    return {k: dict(v) for k, v in _RL_CREDITS.items()}


def reset_rl_credits() -> None:
    """Clear all RL credit state. For test isolation only."""
    _RL_CREDITS.clear()
