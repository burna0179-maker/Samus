"""Karma engine — decay, reward/penalty, and the autonomy check.

Builds on ``backend.strategy.trust_scorer``: the per-actor :class:`KarmaVector`
(four trust dimensions) is composed by ``score_trust`` into an ``AccessTier``,
and ``check`` compares that tier against the tier an action requires.

Math:
  * **decay** — on every load, each dimension is pulled toward the baseline by
    exponential time-decay (deviation halves every ``HALF_LIFE_DAYS`` idle
    days), so neither a hot streak nor a bad patch locks in.
  * **reward/penalty** — ``apply_outcome`` nudges the relevant dimension(s)
    via an EMA step toward 1.0 (reward) or 0.0 (penalty), scaled by the
    outcome's weight and the caller's magnitude.

Every delta is written through ``backend.common.audit.record`` (HMAC-chained).
``apply_outcome`` runs even when the gate is *disabled* so a real track record
accrues before the operator arms enforcement; ``check`` is a no-op ALLOW until
``settings.karma_gate_enabled`` is true.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from backend.common.config import get_settings
from backend.governance.karma import store as karma_store
from backend.governance.karma.store import DEFAULT_BASELINE, KarmaVector
from backend.strategy.trust_scorer import (
    AccessTier,
    TrustInputs,
    can_access,
    score_trust,
)

_LOG = logging.getLogger("samus.governance.karma.engine")

DEFAULT_ACTOR = "samus"
HALF_LIFE_DAYS = 14.0  # deviation from baseline halves every 14 idle days
BASE_ALPHA = 0.15  # EMA step for a unit-magnitude, unit-weight outcome

# outcome -> [(dimension, signed_weight)]; +weight rewards toward 1.0, -penalty
# toward 0.0. Dimensions are exactly trust_scorer's four signals.
_OUTCOMES: dict[str, list[tuple[str, float]]] = {
    "send_success": [("success_rate", +0.5)],
    "reply": [("success_rate", +1.0), ("stability_score", +0.5)],
    "deal_won": [("success_rate", +1.0), ("stability_score", +1.0)],
    "bounce": [("resource_efficiency", -0.6)],
    "complaint": [("policy_compliance", -1.0), ("success_rate", -0.5)],
    "unsubscribe": [("success_rate", -0.3)],
    "policy_block": [("policy_compliance", -1.0)],
    "gate_denied": [("policy_compliance", -0.6)],
    "efh_refusal": [("policy_compliance", -1.0), ("stability_score", -0.5)],
    "heat_critical": [("stability_score", -1.0)],
}

# action_kind -> the AccessTier an actor must hold to take it autonomously.
_REQUIRED_TIER: dict[str, AccessTier] = {
    "outreach_send": AccessTier.AUTONOMOUS,
    "reengagement_send": AccessTier.AUTONOMOUS,
    "auto_stake": AccessTier.AUTONOMOUS,
    "opportunity_write": AccessTier.MULTI_AGENT,
}
_DEFAULT_REQUIRED = AccessTier.MULTI_AGENT


@dataclass(frozen=True)
class KarmaDecision:
    allowed: bool
    actor: str
    action_kind: str
    score: float
    tier: str
    required_tier: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "actor": self.actor,
            "action_kind": self.action_kind,
            "score": self.score,
            "tier": self.tier,
            "required_tier": self.required_tier,
            "reason": self.reason,
        }


def _enabled() -> bool:
    return bool(getattr(get_settings(), "karma_gate_enabled", False))


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _decayed(vec: KarmaVector, baseline: float, now: float) -> KarmaVector:
    if vec.updated_ts <= 0.0 or HALF_LIFE_DAYS <= 0.0:
        return vec
    elapsed_days = max(0.0, (now - vec.updated_ts) / 86400.0)
    if elapsed_days <= 0.0:
        return vec
    retain = 0.5 ** (elapsed_days / HALF_LIFE_DAYS)

    def _d(v: float) -> float:
        return baseline + (v - baseline) * retain

    return KarmaVector(
        actor=vec.actor,
        success_rate=_d(vec.success_rate),
        policy_compliance=_d(vec.policy_compliance),
        resource_efficiency=_d(vec.resource_efficiency),
        stability_score=_d(vec.stability_score),
        updated_ts=vec.updated_ts,
    )


def _to_inputs(vec: KarmaVector) -> TrustInputs:
    return TrustInputs(
        success_rate=vec.success_rate,
        policy_compliance=vec.policy_compliance,
        resource_efficiency=vec.resource_efficiency,
        stability_score=vec.stability_score,
    )


def required_tier(action_kind: str) -> AccessTier:
    return _REQUIRED_TIER.get((action_kind or "").strip(), _DEFAULT_REQUIRED)


def check(
    action_kind: str,
    *,
    actor: str = DEFAULT_ACTOR,
    now: float | None = None,
) -> KarmaDecision:
    """Decide whether ``actor`` may take ``action_kind`` autonomously.

    No-op ALLOW when the gate is disabled. Fail-OPEN to the innocent baseline
    (allow) on any store/engine error — the gate is opt-in + revenue-critical,
    so it only ever restricts a *known*-bad actor, never halts on an outage.
    """
    req = required_tier(action_kind)
    if not _enabled():
        return KarmaDecision(True, actor, action_kind, 1.0, "disabled", req.value, "karma_disabled")
    try:
        store = karma_store.get_store()
        vec = _decayed(store.load(actor), store.baseline, _now(now))
        result = score_trust(_to_inputs(vec))
        allowed = can_access(req, result.tier)
        return KarmaDecision(
            allowed=allowed,
            actor=actor,
            action_kind=action_kind,
            score=result.score,
            tier=result.tier.value,
            required_tier=req.value,
            reason="ok" if allowed else f"insufficient_karma: {result.tier.value} < {req.value}",
        )
    except Exception as exc:  # noqa: BLE001 — fail-open to innocent
        _LOG.warning("karma check errored (allowing, innocent default): %s", exc)
        return KarmaDecision(
            True,
            actor,
            action_kind,
            DEFAULT_BASELINE,
            "autonomous",
            req.value,
            f"karma_error_failopen: {exc}",
        )


def apply_outcome(
    outcome: str,
    *,
    actor: str = DEFAULT_ACTOR,
    magnitude: float = 1.0,
    now: float | None = None,
    ref: str = "",
) -> None:
    """Reward/penalize an actor's karma for an outcome. Best-effort, never raises.

    Runs regardless of the gate flag so a track record accrues before arming.
    Each delta is written to the HMAC-chained audit ledger.
    """
    nudges = _OUTCOMES.get((outcome or "").strip())
    if not nudges:
        return
    try:
        store = karma_store.get_store()
        ts = _now(now)
        vec = _decayed(store.load(actor), store.baseline, ts)
        before = vec.dims()
        for dim, weight in nudges:
            cur = getattr(vec, dim)
            target = 1.0 if weight > 0 else 0.0
            alpha = min(1.0, BASE_ALPHA * abs(weight) * max(0.0, magnitude))
            setattr(vec, dim, max(0.0, min(1.0, cur + (target - cur) * alpha)))
        vec.updated_ts = ts
        store.save(vec)
        _audit_delta(actor, outcome, before, vec.dims(), ref)
    except Exception as exc:  # noqa: BLE001 — bookkeeping never breaks a caller
        _LOG.warning("karma apply_outcome(%s) failed: %s", outcome, exc)


def _audit_delta(
    actor: str,
    outcome: str,
    before: dict,
    after: dict,
    ref: str,
) -> None:
    try:
        from backend.common import audit

        audit.record(
            "karma.delta",
            actor=actor,
            outcome=outcome,
            before=before,
            after=after,
            ref=ref,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("karma audit append failed: %s", exc)


def snapshot(actor: str = DEFAULT_ACTOR, *, now: float | None = None) -> dict:
    store = karma_store.get_store()
    vec = _decayed(store.load(actor), store.baseline, _now(now))
    result = score_trust(_to_inputs(vec))
    return {
        "enabled": _enabled(),
        "actor": actor,
        "score": result.score,
        "tier": result.tier.value,
        "dimensions": vec.dims(),
    }


__all__ = [
    "KarmaDecision",
    "check",
    "apply_outcome",
    "required_tier",
    "snapshot",
    "DEFAULT_ACTOR",
]
