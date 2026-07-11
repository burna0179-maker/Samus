"""Guidance data models — the structured shape of an ingested OpenAI recommendation.

Pure module: dataclasses + enums + (de)serialization only. NO filesystem, NO
network, NO settings. Mirrors the ``cycle_models.py`` convention (the models
live apart from the engine that operates on them).

A :class:`GuidanceRecord` is ONE recommendation Samus received from its strategic
advisor (OpenAI), carried through the Guidance Ingestion Process defined in the
operator's Strategic Daily Intelligence and Guidance Cycle directive
(``data/identity/directives/strategic_intelligence_cycle.md``):

    1. Classify          -> ``category`` + ``tier``
    2. Assess feasibility -> ``feasibility``
    3. Assess impact      -> ``expected_impact``
    4. Assess risk        -> ``risk_level``
    5. Action plan        -> ``action_plan`` (+ ``acceptance_hint``)
    6. Assign ownership   -> ``owner``
    7. Track status       -> ``status`` (lifecycle)

and the Guidance Effectiveness Tracking ledger fields (filled in later, after
the recommendation has been acted on):

    Recommendation       -> ``recommendation``
    Implemented Actions  -> ``implemented_actions``
    Outcome              -> ``outcome``
    Impact               -> ``impact_actual``
    Success Score        -> ``success_score``
    Lessons Learned      -> ``lessons_learned``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

# Ledger row discriminator (mirrors ``kind="cognition_proposal"`` in the
# proposals ledger). The guidance ledger only ever acts on rows it stamped.
KIND = "guidance_record"


class GuidanceStatus(str, Enum):
    """Lifecycle of one recommendation.

    PROPOSED -> (ACCEPTED | REJECTED) -> IN_PROGRESS -> (COMPLETED | ABANDONED)

    A recommendation is born PROPOSED at ingestion. Acceptance is a DELIBERATE
    transition (wire-not-arm: ingestion assesses and hints, but does not
    auto-act). Only an accepted recommendation is converted into an action plan
    and started; only a started one can complete with an effectiveness score.
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


# Statuses past which no further work happens (effectiveness summary treats
# these as closed). REJECTED/ABANDONED are terminal-without-outcome; COMPLETED
# is terminal-with-outcome.
TERMINAL_STATUSES = frozenset(
    {GuidanceStatus.REJECTED.value, GuidanceStatus.COMPLETED.value, GuidanceStatus.ABANDONED.value}
)


class GuidanceTier(int, Enum):
    """Prioritization tier from the directive's framework."""

    CRITICAL = 1  # immediate impact to production / stability / strategy
    HIGH_VALUE = 2  # significant opportunity / optimization / risk
    INFORMATIONAL = 3  # useful but limited immediate impact


class GuidanceCategory(str, Enum):
    """The guidance subject — drives default ownership routing."""

    DAILY_GOAL = "daily_goal"
    WEEKLY_GOAL = "weekly_goal"
    MONTHLY_GOAL = "monthly_goal"
    REVENUE = "revenue_acceleration"
    OPERATIONAL = "operational_optimization"
    AUTONOMY = "autonomy_advancement"
    RESOURCE = "resource_efficiency"
    GOVERNANCE = "governance_improvement"
    CAPABILITY = "capability_expansion"
    RISK = "risk_reduction"
    OTHER = "other"

    @classmethod
    def coerce(cls, raw: Any) -> "GuidanceCategory":
        """Best-effort map an arbitrary string onto a category (never raises)."""
        if isinstance(raw, GuidanceCategory):
            return raw
        text = str(raw or "").strip().lower().replace(" ", "_")
        if not text:
            return cls.OTHER
        # exact value match
        for member in cls:
            if text == member.value:
                return member
        # substring heuristics — tolerant of OpenAI's free-form labels
        table = [
            ("daily", cls.DAILY_GOAL),
            ("weekly", cls.WEEKLY_GOAL),
            ("monthly", cls.MONTHLY_GOAL),
            ("revenue", cls.REVENUE),
            ("operational", cls.OPERATIONAL),
            ("optimization", cls.OPERATIONAL),
            ("autonomy", cls.AUTONOMY),
            ("resource", cls.RESOURCE),
            ("efficiency", cls.RESOURCE),
            ("governance", cls.GOVERNANCE),
            ("capability", cls.CAPABILITY),
            ("risk", cls.RISK),
        ]
        for needle, member in table:
            if needle in text:
                return member
        return cls.OTHER


def _safe_int(raw: Any, default: int) -> int:
    """Coerce to int, tolerant of strings/floats/None (never raises)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float(raw: Any) -> Optional[float]:
    """Coerce to float or None (never raises)."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# Three-level assessment scale used for feasibility / impact / risk.
LEVELS = ("low", "medium", "high")


def coerce_level(raw: Any, default: str = "medium") -> str:
    """Normalize an assessment value to one of ``low|medium|high``."""
    text = str(raw or "").strip().lower()
    if text in LEVELS:
        return text
    # tolerate common synonyms
    if text in ("none", "minimal", "very_low"):
        return "low"
    if text in ("moderate", "med", "mid"):
        return "medium"
    if text in ("critical", "very_high", "severe", "major"):
        return "high"
    return default


# Default ownership routing: which workcell implements a recommendation of a
# given category. Used by the ingestion step (directive step 6).
DEFAULT_OWNER_BY_CATEGORY = {
    GuidanceCategory.DAILY_GOAL: "prospecting",
    GuidanceCategory.WEEKLY_GOAL: "strategy",
    GuidanceCategory.MONTHLY_GOAL: "strategy",
    GuidanceCategory.REVENUE: "outreach",
    GuidanceCategory.OPERATIONAL: "gateway",
    GuidanceCategory.AUTONOMY: "cognition",
    GuidanceCategory.RESOURCE: "finance",
    GuidanceCategory.GOVERNANCE: "governance",
    GuidanceCategory.CAPABILITY: "strategy",
    GuidanceCategory.RISK: "finance",
    GuidanceCategory.OTHER: "operator",
}


@dataclass
class GuidanceRecord:
    """One ingested recommendation, carried through its full lifecycle."""

    recommendation_id: str
    briefing_id: str
    ts: str  # ISO-8601 creation time
    updated_ts: str  # ISO-8601 last-mutation time

    recommendation: str  # the guidance text (what to do)
    rationale: str = ""  # why (advisor's reasoning, if supplied)

    category: str = GuidanceCategory.OTHER.value
    tier: int = GuidanceTier.INFORMATIONAL.value

    # Assessment (directive steps 2-4)
    feasibility: str = "medium"  # low | medium | high
    expected_impact: str = "medium"  # low | medium | high
    risk_level: str = "medium"  # low | medium | high

    # Action plan (directive steps 5-6)
    action_plan: List[str] = field(default_factory=list)
    owner: str = "operator"
    acceptance_hint: str = ""  # accept | reject | review (advisory only)

    status: str = GuidanceStatus.PROPOSED.value

    # Monotonic per-recommendation revision. Incremented on every status
    # transition. The ledger reduces history to current state by MAX(revision)
    # per recommendation_id — NOT by scan order — so latest-wins is correct on
    # the Firestore backend (whose order_by(time.time()) is wall-clock and has
    # undefined tie ordering). See GuidanceLedger.all_latest().
    revision: int = 0

    # Effectiveness tracking (filled in after action)
    implemented_actions: List[str] = field(default_factory=list)
    outcome: Optional[str] = None
    impact_actual: Optional[str] = None  # low | medium | high
    success_score: Optional[float] = None  # 0.0 .. 1.0
    lessons_learned: Optional[str] = None

    # Provenance
    source_question: Optional[str] = None  # which briefing question it answers
    kind: str = KIND

    def to_dict(self) -> dict:
        """Serialize to a plain dict for the JSONL/Firestore ledger."""
        return {
            "kind": self.kind,
            "recommendation_id": self.recommendation_id,
            "briefing_id": self.briefing_id,
            "ts": self.ts,
            "updated_ts": self.updated_ts,
            "recommendation": self.recommendation,
            "rationale": self.rationale,
            "category": self.category,
            "tier": self.tier,
            "feasibility": self.feasibility,
            "expected_impact": self.expected_impact,
            "risk_level": self.risk_level,
            "action_plan": list(self.action_plan),
            "owner": self.owner,
            "acceptance_hint": self.acceptance_hint,
            "status": self.status,
            "revision": self.revision,
            "implemented_actions": list(self.implemented_actions),
            "outcome": self.outcome,
            "impact_actual": self.impact_actual,
            "success_score": self.success_score,
            "lessons_learned": self.lessons_learned,
            "source_question": self.source_question,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "GuidanceRecord":
        """Rebuild a record from a ledger row (tolerant of missing keys)."""
        return cls(
            recommendation_id=str(row.get("recommendation_id", "")),
            briefing_id=str(row.get("briefing_id", "")),
            ts=str(row.get("ts", "")),
            updated_ts=str(row.get("updated_ts", row.get("ts", ""))),
            recommendation=str(row.get("recommendation", "")),
            rationale=str(row.get("rationale", "") or ""),
            category=str(row.get("category", GuidanceCategory.OTHER.value)),
            tier=_safe_int(row.get("tier", GuidanceTier.INFORMATIONAL.value), 3),
            feasibility=str(row.get("feasibility", "medium")),
            expected_impact=str(row.get("expected_impact", "medium")),
            risk_level=str(row.get("risk_level", "medium")),
            action_plan=list(row.get("action_plan", []) or []),
            owner=str(row.get("owner", "operator")),
            acceptance_hint=str(row.get("acceptance_hint", "") or ""),
            status=str(row.get("status", GuidanceStatus.PROPOSED.value)),
            revision=_safe_int(row.get("revision", 0), 0),
            implemented_actions=list(row.get("implemented_actions", []) or []),
            outcome=row.get("outcome"),
            impact_actual=row.get("impact_actual"),
            success_score=_safe_float(row.get("success_score")),
            lessons_learned=row.get("lessons_learned"),
            source_question=row.get("source_question"),
            kind=str(row.get("kind", KIND)),
        )

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


__all__ = [
    "KIND",
    "LEVELS",
    "TERMINAL_STATUSES",
    "DEFAULT_OWNER_BY_CATEGORY",
    "GuidanceStatus",
    "GuidanceTier",
    "GuidanceCategory",
    "GuidanceRecord",
    "coerce_level",
]
