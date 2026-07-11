"""Hustleforge self-marketing package.

Exposes the campaign config, orchestrator, and brand monitor for use by
routes, cron hooks, and tests.
"""
from __future__ import annotations

from backend.marketing.brand_brief import (
    BrandBrief,
    generate_brand_brief,
    load_brand_brief,
    scrub_invented_numbers,
    to_voice_prompt,
)
from backend.marketing.brand_monitor import (
    detect_ai_referrer,
    get_mention_stats,
    log_ai_referral,
)
from backend.marketing.campaign_cycle import CampaignResult, run_monthly_campaign
from backend.marketing.self_campaign import HUSTLEFORGE_CAMPAIGN, SelfMarketingCampaign

__all__ = [
    "SelfMarketingCampaign",
    "HUSTLEFORGE_CAMPAIGN",
    "run_monthly_campaign",
    "CampaignResult",
    "BrandBrief",
    "generate_brand_brief",
    "load_brand_brief",
    "scrub_invented_numbers",
    "to_voice_prompt",
    "log_ai_referral",
    "get_mention_stats",
    "detect_ai_referrer",
]
