#!/usr/bin/env python3
"""
Sales Pipeline — multi-node conversation flow with qualification scoring + branching
Source: ChatGPT recovery chat 06 (full sales pipeline w/ calendar integration)

Canonical relationship:
- [NEW pack] business/sales — full conversation graph (10 nodes + exit + branches)
- [EXPANDS §6 agents.cognitive] adds explicit node/branch graph (parallel to FSM in autonomous_closer)
- Pairs with: deal_scoring_agent (scoring math) + crm_feedback_engine (outcomes)

Lead score thresholds:
  0-6   → unqualified (disqualify exit)
  7-9   → nurture path
  10-15 → sales-ready (book call)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class NodeID(str, Enum):
    ENTRY = "node_1_entry"
    LOW_FRICTION = "node_1b_low_friction"
    Q1_LEAD_GEN = "node_2_q1_lead_gen"
    Q2_VOLUME = "node_3_q2_volume"
    Q3_CONVERSION = "node_4_q3_conversion"
    Q4_BOTTLENECK = "node_5_q4_bottleneck"
    Q5_INTENT = "node_6_q5_intent"
    QUALIFICATION = "node_7_qualification"
    DISQUALIFY = "node_7b_disqualify"
    VALUE_POSITION = "node_8_value_position"
    SOFT_CLOSE = "node_9_soft_close"
    OBJECTION = "node_9b_objection"
    CALENDAR = "node_10_calendar"
    EXIT = "node_exit"


@dataclass
class ScoreRule:
    keywords: List[str]
    points: int


@dataclass
class Node:
    id: NodeID
    message: str
    scoring: List[ScoreRule] = field(default_factory=list)
    next_branches: Dict[str, NodeID] = field(default_factory=dict)
    response_key: Optional[str] = None      # where to store user response in state


@dataclass
class PipelineState:
    lead_score: int = 0
    responses: Dict[str, str] = field(default_factory=dict)
    qualified: bool = False
    current_node: NodeID = NodeID.ENTRY
    status: str = "active"                  # active | nurture | booked | exited
    booking_link: str = "https://calendly.com/hustleforge/strategy-call"


NODES: Dict[NodeID, Node] = {
    NodeID.ENTRY: Node(
        id=NodeID.ENTRY,
        message="Hello, this is Morgan from GrowthPartners. Do you have a few minutes to chat about how we might be able to help your business?",
        next_branches={"yes": NodeID.Q1_LEAD_GEN, "maybe": NodeID.LOW_FRICTION, "no": NodeID.EXIT},
    ),
    NodeID.LOW_FRICTION: Node(
        id=NodeID.LOW_FRICTION,
        message="No problem at all — I can keep it quick. We help businesses generate more consistent inbound leads and automate follow-up so nothing slips through. Would it be okay if I asked a couple quick questions to see if it's even relevant for you?",
        next_branches={"yes": NodeID.Q1_LEAD_GEN, "no": NodeID.EXIT},
    ),
    NodeID.Q1_LEAD_GEN: Node(
        id=NodeID.Q1_LEAD_GEN,
        message="Out of curiosity, how are you currently generating new leads or customers?",
        response_key="lead_gen",
        scoring=[
            ScoreRule(["none", "no system", "word of mouth"], 3),
            ScoreRule(["ads", "seo", "referral"], 2),
            ScoreRule(["multi-channel", "everything"], 1),
        ],
        next_branches={"_default": NodeID.Q2_VOLUME},
    ),
    NodeID.Q2_VOLUME: Node(
        id=NodeID.Q2_VOLUME,
        message="Roughly how many inbound leads or inquiries are you getting in a typical month?",
        response_key="volume",
        # Note: numeric scoring handled outside keyword rules (see score_volume)
        next_branches={"_default": NodeID.Q3_CONVERSION},
    ),
    NodeID.Q3_CONVERSION: Node(
        id=NodeID.Q3_CONVERSION,
        message="When a lead comes in, what does your follow-up process look like?",
        response_key="conversion",
        scoring=[
            ScoreRule(["no system", "inconsistent", "manual"], 3),
            ScoreRule(["manual follow-up", "spreadsheet"], 2),
            ScoreRule(["crm", "automated"], 1),
        ],
        next_branches={"_default": NodeID.Q4_BOTTLENECK},
    ),
    NodeID.Q4_BOTTLENECK: Node(
        id=NodeID.Q4_BOTTLENECK,
        message="What would you say is the biggest challenge right now when it comes to growing your business?",
        response_key="bottleneck",
        scoring=[
            ScoreRule(["lead", "conversion", "scale", "scaling"], 3),
            ScoreRule(["growth", "marketing"], 2),
        ],
        next_branches={"_default": NodeID.Q5_INTENT},
    ),
    NodeID.Q5_INTENT: Node(
        id=NodeID.Q5_INTENT,
        message="Are you actively looking to grow right now, or just exploring options?",
        response_key="intent",
        scoring=[
            ScoreRule(["actively", "scaling", "now"], 3),
            ScoreRule(["interested", "open"], 2),
            ScoreRule(["browsing", "just looking"], 0),
        ],
        next_branches={"_default": NodeID.QUALIFICATION},
    ),
    NodeID.QUALIFICATION: Node(
        id=NodeID.QUALIFICATION,
        message="",
        next_branches={"qualified": NodeID.VALUE_POSITION, "unqualified": NodeID.DISQUALIFY},
    ),
    NodeID.DISQUALIFY: Node(
        id=NodeID.DISQUALIFY,
        message="Got it — based on what you shared, it sounds like this might not be the right fit at the moment. If things change or you decide to focus more on growth systems, feel free to reach out.",
        next_branches={"_default": NodeID.EXIT},
    ),
    NodeID.VALUE_POSITION: Node(
        id=NodeID.VALUE_POSITION,
        message="Based on what you mentioned, it sounds like there's a gap we can close. We help businesses: generate more consistent inbound leads, automate follow-up, and improve conversion rates without increasing ad spend.",
        next_branches={"_default": NodeID.SOFT_CLOSE},
    ),
    NodeID.SOFT_CLOSE: Node(
        id=NodeID.SOFT_CLOSE,
        message="Would it make sense to schedule a quick strategy call to walk through what this could look like for you?",
        next_branches={"yes": NodeID.CALENDAR, "objection": NodeID.OBJECTION, "no": NodeID.EXIT},
    ),
    NodeID.OBJECTION: Node(
        id=NodeID.OBJECTION,
        message="Totally fair. Most people I speak with feel the same at first — that's why the call is just a quick walkthrough, no pressure. If it's not a fit, at least you'll walk away with a clearer strategy.",
        next_branches={"yes": NodeID.CALENDAR, "no": NodeID.EXIT},
    ),
    NodeID.CALENDAR: Node(
        id=NodeID.CALENDAR,
        message="Great — here's a link to grab a time that works best for you: {booking_link}",
        next_branches={"_default": NodeID.EXIT},
    ),
    NodeID.EXIT: Node(
        id=NodeID.EXIT,
        message="Appreciate your time. If you ever want to revisit this, feel free to reach out.",
    ),
}


def score_volume(text: str) -> int:
    """Numeric volume scoring (separate from keyword scoring)."""
    import re
    m = re.search(r"\d+", text or "")
    if not m:
        return 0
    n = int(m.group())
    if n < 10:
        return 3
    if n < 50:
        return 2
    return 1


def evaluate_qualification(state: PipelineState) -> bool:
    return state.lead_score >= 10


def apply_scoring(node: Node, user_input: str, state: PipelineState) -> None:
    if node.response_key:
        state.responses[node.response_key] = user_input
    text = (user_input or "").lower()
    if node.id == NodeID.Q2_VOLUME:
        state.lead_score += score_volume(user_input)
        return
    for rule in node.scoring:
        if any(kw in text for kw in rule.keywords):
            state.lead_score += rule.points
            return


def next_node(node: Node, user_input: str, state: PipelineState) -> NodeID:
    text = (user_input or "").lower()
    if node.id == NodeID.QUALIFICATION:
        return node.next_branches["qualified" if evaluate_qualification(state) else "unqualified"]
    for branch_key, target in node.next_branches.items():
        if branch_key == "_default":
            continue
        if branch_key in text:
            return target
    return node.next_branches.get("_default", NodeID.EXIT)


def advance(state: PipelineState, user_input: str) -> Dict[str, Any]:
    node = NODES[state.current_node]
    apply_scoring(node, user_input, state)
    state.current_node = next_node(node, user_input, state)
    next_n = NODES[state.current_node]
    msg = next_n.message.format(booking_link=state.booking_link) if "{booking_link}" in next_n.message else next_n.message

    # Status side effects
    if state.current_node == NodeID.CALENDAR:
        state.status = "booked"
    elif state.current_node == NodeID.DISQUALIFY:
        if 7 <= state.lead_score <= 9:
            state.status = "nurture"
        else:
            state.status = "exited"

    return {
        "node": state.current_node.value,
        "message": msg,
        "lead_score": state.lead_score,
        "status": state.status,
    }


# CRM sync fields (recommended schema for downstream CRM table):
CRM_FIELDS = [
    "name", "business_name", "lead_source", "lead_score",
    "bottleneck", "monthly_leads", "follow_up_system", "intent_level",
    "status",   # new | qualified | booked | closed
]


# Webhook hooks (target services):
WEBHOOK_TARGETS = {
    "calendly_booked": "outreach.send_close",
    "stripe_paid": "fulfillment.plan_execution",
    "lead_qualified": "outreach.send_outreach",
}
