#!/usr/bin/env python3
"""
AI Receptionist Pod — SMMS gateway / front-door automation
Source: ChatGPT recovery chat 34

Canonical relationship:
- [NEW pack] business/smms — multi-channel intake pod
- [EXPANDS §6 agents] gateway pod (front-door before any business pod handoff)
- [EXPANDS §6 application] cross-channel intake (web chat / IG DM / TikTok DM / SMS / email / WhatsApp / phone)
- [INTEGRATES] callsheet_product_registry + deal_scoring_agent + autonomous_closer (handoff target)

Pod surface (pod_receptionist/):
  config.json                — pod config + supported channels
  receptionist_brain.json    — opening lines, scoring weights, routing table
  receptionist_router.py     — channel → pod handoff
  create_intake_file.py      — auto-write intake.json
  scoring_engine.py          — 1-5 lead score (this file)
  voicemail_transcriber.py   — Twilio Voice → Whisper
  send_to_pipeline.py        — dispatch into SMMS pods

Voice path: Twilio Voice → Whisper STT → receptionist pod → response → TTS
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class Channel(str, Enum):
    WEB_CHAT = "web_chat"
    INSTAGRAM_DM = "instagram_dm"
    TIKTOK_DM = "tiktok_dm"
    SMS = "sms"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    PHONE = "phone"


class ServiceInterest(str, Enum):
    SMMS_AUTOMATION = "smms_automation"
    CONTENT_SYSTEMS = "content_systems"
    CUSTOM_WORKFLOWS = "custom_workflows"
    INFO_ONLY = "info_only"
    UNKNOWN = "unknown"


@dataclass
class IntakeRecord:
    name: str = ""
    contact: str = ""
    lead_score: int = 0
    service_interest: ServiceInterest = ServiceInterest.UNKNOWN
    timeline: str = ""           # "asap" | "this_month" | "next_30" | "next_90" | "exploring"
    budget: str = ""             # "under_150" | "150_500" | "500_2k" | "2k_5k" | "5k_plus"
    preferred_platform: str = ""
    source: Channel = Channel.WEB_CHAT
    intake_id: str = ""
    created_at: float = 0.0


@dataclass
class ScoringInputs:
    budget_tier: int = 0        # 0..5 (0=none, 5=high)
    urgency: int = 0            # 0..5
    business_size: int = 0      # 0..5
    clear_use_case: bool = False
    has_existing_tools: bool = False


def score_lead(inputs: ScoringInputs) -> int:
    """Returns 1..5 score. >=5 = hot, 3-4 = warm, 1-2 = cold."""
    raw = (
        inputs.budget_tier * 0.30
        + inputs.urgency * 0.30
        + inputs.business_size * 0.20
        + (2 if inputs.clear_use_case else 0) * 0.10
        + (1 if inputs.has_existing_tools else 0) * 0.10
    )
    return max(1, min(5, round(raw)))


# ----- Routing table -----
ROUTING_TABLE = {
    ServiceInterest.CONTENT_SYSTEMS:   "smms_content_pod",
    ServiceInterest.SMMS_AUTOMATION:   "smms_automation_pod",
    ServiceInterest.CUSTOM_WORKFLOWS:  "hf_ops_pod",
    ServiceInterest.INFO_ONLY:         "ai_warmup_drip",
    ServiceInterest.UNKNOWN:           "ai_warmup_drip",
}


def route_by_score_and_interest(score: int, interest: ServiceInterest) -> str:
    """Hot leads bypass content routing → direct operator handoff."""
    if score >= 5:
        return "direct_operator_handoff"
    if score >= 3:
        return ROUTING_TABLE.get(interest, "ai_warmup_drip")
    return "ai_warmup_drip"


# ----- Channel-specific openers -----
OPENERS = {
    Channel.WEB_CHAT: "Hey there 👋 welcome to HustleForge. What brings you in today — automation help, social media systems, or something else?",
    Channel.INSTAGRAM_DM: "Hey! I can help you with automation, SMMS setup, or scaling your brand. What are you trying to accomplish?",
    Channel.TIKTOK_DM: "Hey! I can help you with automation, SMMS setup, or scaling your brand. What are you trying to accomplish?",
    Channel.PHONE: "Thanks for calling HustleForge. Quick question so I can get you to the right person — are you looking for automation tools, content systems, or support you're already using?",
    Channel.SMS: "Hey, this is HustleForge. What can we help you with — automation or content systems?",
    Channel.EMAIL: "Hey there — thanks for reaching out to HustleForge. Quick question to help route you: automation, content, or custom workflows?",
    Channel.WHATSAPP: "Hey! HustleForge here. Are you looking for automation, content systems, or something custom?",
}


# ----- Receptionist pod -----
class AIReceptionistPod:
    POD_ID = "pod_receptionist"

    def __init__(self, intake_dir: Path, dispatcher=None):
        self.intake_dir = Path(intake_dir)
        self.intake_dir.mkdir(parents=True, exist_ok=True)
        self.dispatcher = dispatcher

    def greet(self, channel: Channel) -> str:
        return OPENERS.get(channel, OPENERS[Channel.WEB_CHAT])

    def process_interaction(self, channel: Channel, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Single interaction → score + route + intake."""
        inputs = ScoringInputs(
            budget_tier=int(payload.get("budget_tier", 0)),
            urgency=int(payload.get("urgency", 0)),
            business_size=int(payload.get("business_size", 0)),
            clear_use_case=bool(payload.get("clear_use_case", False)),
            has_existing_tools=bool(payload.get("has_existing_tools", False)),
        )
        score = score_lead(inputs)
        interest = ServiceInterest(payload.get("service_interest", "unknown"))
        route_target = route_by_score_and_interest(score, interest)

        intake = IntakeRecord(
            name=payload.get("name", ""),
            contact=payload.get("contact", ""),
            lead_score=score,
            service_interest=interest,
            timeline=payload.get("timeline", ""),
            budget=payload.get("budget", ""),
            preferred_platform=payload.get("preferred_platform", ""),
            source=channel,
            intake_id=f"intake-{uuid.uuid4().hex[:12]}",
            created_at=time.time(),
        )
        self._write_intake(intake)

        if self.dispatcher:
            self.dispatcher(target=route_target, payload=asdict(intake))

        return {
            "intake_id": intake.intake_id,
            "score": score,
            "route_target": route_target,
            "actions": self._actions_for_score(score),
        }

    def _write_intake(self, intake: IntakeRecord) -> None:
        path = self.intake_dir / f"{intake.intake_id}.json"
        data = asdict(intake)
        data["service_interest"] = intake.service_interest.value
        data["source"] = intake.source.value
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _actions_for_score(self, score: int) -> List[str]:
        if score >= 5:
            return ["send_calendly_link", "send_packages_pdf", "sms_confirmation",
                    "smms_onboarding_wizard", "direct_operator_alert"]
        if score >= 3:
            return ["send_packages_pdf", "email_followup_sequence"]
        return ["newsletter_drip"]


# ----- SMMS pricing tiers (for reference; commercial design) -----
SMMS_PRICING = {
    "Starter":    {"monthly": 197,  "setup": 299,  "internal_cost": 12,  "hidden_fees": 8},
    "Growth":     {"monthly": 497,  "setup": 799,  "internal_cost": 35,  "hidden_fees": 15},
    "Scale":      {"monthly": 997,  "setup": 1500, "internal_cost": 70,  "hidden_fees": 35},
    "Enterprise": {"monthly": 2500, "setup": 0,    "internal_cost": 260, "hidden_fees": 110},
}

ADDON_PRICING = {
    "AI Receptionist":   {"monthly": 99, "internal_cost": 3,  "hidden_fees": 2},
    "Automation Pack":   {"one_time": 149, "internal_cost": 0, "hidden_fees": 0},
    "Side Hustle Kit":   {"one_time": 59,  "internal_cost": 0, "hidden_fees": 0},
    "Content Pack":      {"one_time": 29,  "internal_cost": 0, "hidden_fees": 0},
}
