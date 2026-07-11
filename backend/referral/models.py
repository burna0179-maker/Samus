"""Dataclasses for the referral loop. Stdlib-only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReferralStatus = Literal["pending", "qualified", "rewarded", "rejected"]


@dataclass
class ReferralProgram:
    """Program configuration. Rewards are described as opaque strings (e.g.
    "$50 credit", "1 month free") so the engine stays payment-agnostic."""

    name: str = "Hustleforge Referrals"
    base_url: str = "https://hustleforge.ai"
    referrer_reward: str = "$100 credit"
    referee_reward: str = "$50 credit"
    qualify_event: str = "subscription_started"  # what makes a referral qualify
    code_salt: str = "hf"


@dataclass
class Referral:
    """One referral relationship: a referrer's code claimed by a referee."""

    code: str
    referrer_id: str
    referee_id: str
    status: ReferralStatus = "pending"
    referrer_reward: str = ""
    referee_reward: str = ""
    created_at: str = ""
    qualified_at: str = ""
