"""DecayRiskScore — the signal_decay trigger's core computation.

The trigger is not a timer and not a bare score threshold. It is *signal
decay*: a deal loses value the longer it sits untouched, and how fast it
decays depends on where it is in the pipeline. A proposal that has gone
silent for a week is bleeding; a brand-new lead sitting quiet is barely
warm. This module turns three inputs into one 0.0-1.0 risk number:

  1. the current Opportunity stage          (how much is at stake to lose)
  2. time since the last communication       (how cold it has gone)
  3. external factors (market / regulatory)  (pluggable; raises risk only)

The function is pure given an injected ``now`` so the trigger stays
deterministic and testable. It performs no I/O — the caller supplies the
Opportunity, the (optional) CallState, and any external-factor signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.common.dates import utc_now

__all__ = [
    "STAGE_DECAY_WEIGHT",
    "TERMINAL_STAGES",
    "DecayAssessment",
    "compute_decay_risk",
]

# How much "at risk of being lost to silence" a stalled deal in each stage is
# (0.0-1.0). A sent proposal going cold is the canonical decay event, so it
# carries full weight; an active negotiation is nearly as costly to lose; a
# fresh lead sitting quiet is low-stakes. Terminal stages are not at risk —
# the deal is already resolved.
STAGE_DECAY_WEIGHT: dict[str, float] = {
    "new": 0.25,
    "qualified": 0.50,
    "proposal": 1.00,
    "negotiation": 0.95,
}

TERMINAL_STAGES: frozenset[str] = frozenset(
    {"closed_won", "closed_won_retainer", "closed_lost"}
)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _parse_iso(value: Any) -> datetime | None:
    """Parse a Samus ISO-8601 ``...Z`` timestamp to a tz-aware UTC datetime.

    Returns None on empty / unparseable input — a missing timestamp simply
    drops out of the "latest communication" max rather than crashing the
    trigger.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.strptime(text, _ISO_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class DecayAssessment:
    """The decomposed DecayRiskScore for one Opportunity."""

    opportunity_id: str
    prospect_id: str
    stage: str
    decay_risk: float          # the headline 0.0-1.0 score
    staleness_days: float      # whole-ish days since last known contact
    stall_factor: float        # staleness saturated against the stall window
    stage_weight: float        # STAGE_DECAY_WEIGHT for this stage
    external_factor: float     # clamped external contribution
    is_terminal: bool          # closed deal — never at decay risk

    def crosses(self, threshold: float) -> bool:
        """True when this deal should fire a signal_decay trigger."""
        if self.is_terminal:
            return False
        return self.decay_risk >= threshold


def compute_decay_risk(
    opp: Any,
    *,
    call_state: Any = None,
    external_factor: float = 0.0,
    stall_days: float = 7.0,
    now: datetime | None = None,
) -> DecayAssessment:
    """Compute the DecayRiskScore for ``opp``.

    ``opp`` is a :class:`backend.crm.models.Opportunity` (duck-typed: needs
    ``opportunity_id``, ``prospect_id``, ``stage``, ``updated_at``,
    ``created_at``). ``call_state`` is the optional
    :class:`backend.crm.models.CallState` whose ``last_attempt_at`` is folded
    into the "last communication" signal. ``external_factor`` (0.0-1.0) is a
    pluggable market/regulatory pressure that can only *raise* risk — it
    defaults to 0.0 until a real feed is wired, so today the score is driven
    purely by stage x staleness (honest: no fabricated external signal).

    Score:  base = stage_weight * stall_factor
            risk = base + external * (1 - base)        (monotonic, capped 1)
    """
    stage = str(getattr(opp, "stage", "") or "")
    is_terminal = stage in TERMINAL_STAGES
    stage_weight = 0.0 if is_terminal else STAGE_DECAY_WEIGHT.get(stage, 0.25)

    when = now or utc_now()

    # Latest known contact: the most recent of the Opportunity's own
    # updated_at/created_at and the dialer's last_attempt_at. created_at is
    # always present, so a never-touched deal still yields real staleness.
    candidates: list[datetime] = []
    for ts in (
        getattr(opp, "updated_at", ""),
        getattr(opp, "created_at", ""),
        getattr(call_state, "last_attempt_at", "") if call_state is not None else "",
    ):
        parsed = _parse_iso(ts)
        if parsed is not None:
            candidates.append(parsed)

    if candidates:
        latest = max(candidates)
        staleness_days = max(0.0, (when - latest).total_seconds() / 86400.0)
    else:
        # No parseable timestamp at all — we cannot assert silence, so we do
        # not manufacture decay (the trigger fails toward NOT firing; a
        # manual_review is always available).
        staleness_days = 0.0

    window = stall_days if stall_days > 0 else 7.0
    stall_factor = _clamp01(staleness_days / window)
    ext = _clamp01(float(external_factor or 0.0))

    base = stage_weight * stall_factor
    decay_risk = 0.0 if is_terminal else _clamp01(base + ext * (1.0 - base))

    return DecayAssessment(
        opportunity_id=str(getattr(opp, "opportunity_id", "") or ""),
        prospect_id=str(getattr(opp, "prospect_id", "") or ""),
        stage=stage,
        decay_risk=decay_risk,
        staleness_days=staleness_days,
        stall_factor=stall_factor,
        stage_weight=stage_weight,
        external_factor=ext,
        is_terminal=is_terminal,
    )
