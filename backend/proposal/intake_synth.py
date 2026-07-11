"""Synthesize an OnboardingIntake from cold-prospect signals.

The proposal pipeline is driven by an :class:`OnboardingIntake` — normally
produced by the onboarding form a warm client fills in. A cash-engine review
fires on a *cold* prospect that has no such form, only what the CRM knows
(company, industry, description) plus the operator's Stake Sentence. Rather
than fabricate an intake inside the cash_engine (which would be a second,
divergent proposal-input system), the mapping lives here, in the proposal
workcell that owns the model — so the cash engine just asks the proposal
workcell to make a proposal from the signals it has.

Only ``client_name`` and ``business_goal`` are required by OnboardingIntake;
the want-lists default empty. A proposal generated from this minimal intake
will typically come back ``needs_review`` — which is the honest status for an
auto-seeded proposal a cold prospect hasn't shaped yet: it runs to the
operator, exactly as intended.
"""
from __future__ import annotations

from .models import OnboardingIntake

__all__ = ["synthesize_intake"]

_MAX_GOAL_LEN = 500


def synthesize_intake(
    *,
    company_name: str,
    industry: str = "",
    business_description: str = "",
    stake_sentence: str = "",
) -> OnboardingIntake:
    """Map available cold-prospect signals onto an OnboardingIntake.

    Deterministic and offline. ``stake_sentence`` is folded into the goal as
    the operator's stated angle on why this prospect is worth pursuing.
    """
    name = (company_name or "").strip() or "Prospect"

    parts: list[str] = []
    desc = (business_description or "").strip()
    if desc:
        parts.append(desc)
    ind = (industry or "").strip()
    if ind:
        parts.append(f"grow {ind} revenue")
    stake = (stake_sentence or "").strip()
    if stake:
        parts.append(f"operator angle: {stake}")
    parts.append("act on the gaps surfaced in the cash-engine Gap Report")

    business_goal = "; ".join(parts)[:_MAX_GOAL_LEN]

    return OnboardingIntake(
        client_name=name,
        business_goal=business_goal,
    )
