"""Declarative multi-touch nurture sequences with behavioral branching.

Today the outreach path has a single 2-day follow-up. This adds the missing
layer: day-cadenced sequences (welcome / activation-onboarding / re-engagement)
that branch on prospect behaviour (a click fast-tracks to sales; a trial start
switches to onboarding; silence by day N switches to re-engagement; a reply or
unsubscribe stops the sequence).

The engine is pure and stateless: given a :class:`Sequence` definition and an
:class:`Enrollment` (its history of events + completed steps), it computes the
next action at a point in time. **Dispatch is DRY-RUN by default** — the engine
plans messages; it does not send. Wiring ``plan_next`` to the live outreach send
path + a scheduler is the operator's activation step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Channel = Literal["email", "linkedin", "sms"]
SequenceKind = Literal["welcome", "onboarding", "reengagement", "buying_signal"]

# Behavioral events a prospect can generate against an enrollment.
EventType = Literal[
    "sent",
    "opened",
    "clicked",
    "replied",
    "trial_started",
    "meeting_booked",
    "unsubscribed",
]


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


@dataclass
class Touch:
    """One scheduled message in a sequence. ``day`` is the offset (in whole
    days) from enrollment start."""

    step: int
    day: int
    channel: Channel
    template_id: str
    subject: str = ""
    brief: str = ""


@dataclass
class Branch:
    """A behavioral rule. ``when`` is an event type, or ``"no_open_by_day:N"``.
    ``action`` is one of: ``fast_track``, ``switch:<sequence_id>``, ``stop``."""

    when: str
    action: str


@dataclass
class Sequence:
    id: str
    kind: SequenceKind
    touches: list[Touch]
    branches: list[Branch] = field(default_factory=list)


@dataclass
class SeqEvent:
    type: str
    day: int = 0
    step: int | None = None
    at: str = ""


@dataclass
class Enrollment:
    prospect_id: str
    sequence_id: str
    started_at: str  # ISO 8601
    status: Literal["active", "switched", "stopped", "complete"] = "active"
    completed_steps: list[int] = field(default_factory=list)
    events: list[SeqEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse(iso: str) -> datetime:
    s = (iso or "").strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_elapsed(enrollment: Enrollment, now_iso: str) -> int:
    """Whole days between enrollment start and ``now`` (floored, never < 0)."""
    delta = _parse(now_iso) - _parse(enrollment.started_at)
    return max(0, delta.days)


def has_event(enrollment: Enrollment, event_type: str) -> bool:
    return any(e.type == event_type for e in enrollment.events)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def evaluate_branch(seq: Sequence, enrollment: Enrollment, now_iso: str) -> Branch | None:
    """Return the first matching branch (branch order = priority), or None."""
    elapsed = days_elapsed(enrollment, now_iso)
    for branch in seq.branches:
        when = branch.when
        if when.startswith("no_open_by_day:"):
            try:
                threshold = int(when.split(":", 1)[1])
            except ValueError:
                continue
            if elapsed >= threshold and not has_event(enrollment, "opened"):
                return branch
        elif has_event(enrollment, when):
            return branch
    return None


def due_touches(seq: Sequence, enrollment: Enrollment, now_iso: str) -> list[Touch]:
    """Touches whose day has arrived and that haven't been completed yet."""
    if enrollment.status != "active":
        return []
    elapsed = days_elapsed(enrollment, now_iso)
    done = set(enrollment.completed_steps)
    return sorted(
        (t for t in seq.touches if t.step not in done and t.day <= elapsed),
        key=lambda t: t.step,
    )


def plan_next(seq: Sequence, enrollment: Enrollment, now_iso: str) -> dict:
    """Compute the next action for an enrollment.

    Branches are evaluated first (behaviour overrides cadence). Returns one of:
      * ``{"action": "stop", "reason": <event>}``
      * ``{"action": "switch", "target": <sequence_id>}``
      * ``{"action": "fast_track"}`` (hand to sales; cadence continues)
      * ``{"action": "send", "touch": Touch}``
      * ``{"action": "wait"}`` (enrolled, nothing due yet)
      * ``{"action": "complete"}`` (all touches done)
    """
    if enrollment.status != "active":
        return {"action": enrollment.status}

    all_complete = len(enrollment.completed_steps) >= len(seq.touches)
    branch = evaluate_branch(seq, enrollment, now_iso)
    fast_track = False
    if branch is not None:
        is_cadence = branch.when.startswith("no_open_by_day:")
        if branch.action == "stop":
            # Hard stop (unsubscribe/reply) always wins.
            return {"action": "stop", "reason": branch.when}
        if branch.action.startswith("switch:"):
            # Event-driven switches (trial_started) always win; a cadence
            # diversion (no-open) yields to completion once all touches sent.
            if not (is_cadence and all_complete):
                return {"action": "switch", "target": branch.action.split(":", 1)[1]}
        elif branch.action == "fast_track":
            fast_track = True  # continue cadence, but flag for sales

    due = due_touches(seq, enrollment, now_iso)
    if due:
        result = {"action": "send", "touch": due[0]}
        if fast_track:
            result["fast_track"] = True
        return result
    if all_complete:
        return {"action": "complete"}
    return {"action": "fast_track"} if fast_track else {"action": "wait"}


def record_event(
    enrollment: Enrollment, event_type: str, now_iso: str, *, step: int | None = None
) -> None:
    enrollment.events.append(
        SeqEvent(type=event_type, day=days_elapsed(enrollment, now_iso), step=step, at=now_iso)
    )


def mark_sent(enrollment: Enrollment, touch: Touch, now_iso: str) -> None:
    if touch.step not in enrollment.completed_steps:
        enrollment.completed_steps.append(touch.step)
    record_event(enrollment, "sent", now_iso, step=touch.step)


def render_touch(touch: Touch, enrollment: Enrollment) -> dict:
    """Render a due touch into a planned message (no send)."""
    return {
        "prospect_id": enrollment.prospect_id,
        "sequence_id": enrollment.sequence_id,
        "step": touch.step,
        "channel": touch.channel,
        "template_id": touch.template_id,
        "subject": touch.subject,
        "brief": touch.brief,
    }


def dispatch_due(
    seq: Sequence, enrollment: Enrollment, now_iso: str, *, dry_run: bool = True
) -> dict:
    """Plan (and, when armed, mark) the next due touch. DRY-RUN by default:
    returns the planned message without sending or mutating the enrollment.

    When ``dry_run=False`` the touch is marked sent on the enrollment so the
    next call advances — the actual network send is the caller's responsibility
    (route the returned message through the live outreach send path)."""
    plan = plan_next(seq, enrollment, now_iso)
    if plan["action"] != "send":
        return plan
    touch: Touch = plan["touch"]
    message = render_touch(touch, enrollment)
    message["dry_run"] = dry_run
    if plan.get("fast_track"):
        message["fast_track"] = True
    if not dry_run:
        mark_sent(enrollment, touch, now_iso)
    return {"action": "send", "message": message}


# ---------------------------------------------------------------------------
# Predefined sequences
# ---------------------------------------------------------------------------

# 6-email, 14-day welcome/nurture cadence (Opinly's framework).
WELCOME_SEQUENCE = Sequence(
    id="welcome",
    kind="welcome",
    touches=[
        Touch(1, 0, "email", "welcome_quick_win", subject="Welcome — one quick win", brief="What to expect + ONE immediate-value action. No pitch."),
        Touch(2, 2, "email", "problem_education", subject="The hidden cost of {pain}", brief="Deepen the pain point + a surprising stat."),
        Touch(3, 4, "email", "solution_intro", subject="How teams actually solve this", brief="Introduce the approach; show a result/clip. CTA: 2-min video / trial."),
        Touch(4, 7, "email", "social_proof", subject="{customer} got {result}", brief="Customer story: Problem -> Tried -> Used -> Result. CTA: start trial."),
        Touch(5, 10, "email", "objection", subject="\"But will it work for us?\"", brief="Address the #1 non-conversion reason. CTA: book a 15-min call."),
        Touch(6, 14, "email", "last_touch", subject="Last note", brief="Urgency / trial ending. Two CTAs: upgrade AND book a call."),
    ],
    branches=[
        Branch("unsubscribed", "stop"),
        Branch("replied", "stop"),
        Branch("trial_started", "switch:onboarding"),
        Branch("clicked", "fast_track"),
        Branch("no_open_by_day:7", "switch:reengagement"),
    ],
)

# Activation onboarding — get to first value within 48h.
ONBOARDING_SEQUENCE = Sequence(
    id="onboarding",
    kind="onboarding",
    touches=[
        Touch(1, 0, "email", "onboard_welcome", subject="You're in — let's get your first result", brief="The single action that delivers first value."),
        Touch(2, 1, "email", "onboard_nudge", subject="Your first result is one step away", brief="Nudge toward the aha moment; offer help."),
        Touch(3, 2, "email", "onboard_aha_check", subject="Did you hit {first_value}?", brief="Confirm activation; if not, remove the blocker."),
    ],
    branches=[
        Branch("unsubscribed", "stop"),
        Branch("meeting_booked", "stop"),
    ],
)

# Re-engagement for dormant prospects.
REENGAGEMENT_SEQUENCE = Sequence(
    id="reengagement",
    kind="reengagement",
    touches=[
        Touch(1, 0, "email", "reengage_value", subject="Still wrestling with {pain}?", brief="Pure value, no ask — a useful resource."),
        Touch(2, 3, "email", "reengage_proof", subject="What changed for {customer}", brief="A fresh proof point relevant to their segment."),
        Touch(3, 7, "email", "reengage_breakup", subject="Should I close your file?", brief="Permission-to-close breakup email; easy re-opt-in."),
    ],
    branches=[
        Branch("unsubscribed", "stop"),
        Branch("replied", "stop"),
        Branch("trial_started", "switch:onboarding"),
    ],
)

# Buying-signal warm follow-up — enrolled when a voice call surfaces real intent
# (high intent_score / recommended_action == book_call / tier in high|priority).
# A call already established warmth, so this bypasses the cold warmth gate and
# moves fast: a same-day recap, a next-day nudge, a day-3 soft close.
BUYING_SIGNAL_SEQUENCE = Sequence(
    id="buying_signal",
    kind="buying_signal",
    touches=[
        Touch(1, 0, "email", "buying_signal_recap", subject="Great talking — here's that link", brief="Warm recap of the call referencing the specific pain they named + the offer/checkout link. ONE clear CTA. No re-pitch — they already said yes-ish on the call."),
        Touch(2, 1, "email", "buying_signal_nudge", subject="Anything I can clear up?", brief="Short nudge — offer to answer the one question holding them back. No pressure, single CTA back to the link."),
        Touch(3, 3, "email", "buying_signal_hold", subject="Holding your spot", brief="Soft-urgency close — we kept your spot, last easy step. Permission-to-close tone. Single CTA."),
    ],
    branches=[
        Branch("unsubscribed", "stop"),
        Branch("replied", "stop"),
        Branch("meeting_booked", "stop"),
    ],
)

SEQUENCES: dict[str, Sequence] = {
    s.id: s for s in (
        WELCOME_SEQUENCE,
        ONBOARDING_SEQUENCE,
        REENGAGEMENT_SEQUENCE,
        BUYING_SIGNAL_SEQUENCE,
    )
}


def get_sequence(sequence_id: str) -> Sequence | None:
    return SEQUENCES.get(sequence_id)


# ---------------------------------------------------------------------------
# HTTP-adapter handler (dict in, dict out) — planning only, never sends.
# ---------------------------------------------------------------------------


def _enrollment_from(payload: dict) -> Enrollment | None:
    enr = payload.get("enrollment")
    if not isinstance(enr, dict):
        return None
    events = [
        SeqEvent(
            type=str(e.get("type", "")),
            day=int(e.get("day", 0)),
            step=e.get("step"),
            at=str(e.get("at", "")),
        )
        for e in (enr.get("events") or [])
        if isinstance(e, dict)
    ]
    return Enrollment(
        prospect_id=str(enr.get("prospect_id", "")),
        sequence_id=str(enr.get("sequence_id", payload.get("sequence_id", ""))),
        started_at=str(enr.get("started_at", "")),
        status=enr.get("status", "active"),
        completed_steps=[int(s) for s in (enr.get("completed_steps") or [])],
        events=events,
    )


def handle_plan_nurture(payload: dict) -> dict:
    """Compute the next nurture action for an enrollment. DRY-RUN: plans the
    next touch, never sends and never mutates. Returns ``{error: ...}`` for a
    bad payload rather than raising."""
    sequence_id = str(payload.get("sequence_id") or "")
    seq = get_sequence(sequence_id)
    if seq is None:
        return {"error": f"unknown_sequence:{sequence_id}", "known": list(SEQUENCES)}
    enrollment = _enrollment_from(payload)
    if enrollment is None or not enrollment.started_at:
        return {"error": "enrollment_required"}
    now = str(payload.get("now") or enrollment.started_at)
    out = dispatch_due(seq, enrollment, now, dry_run=True)
    # dispatch_due returns either a plan dict (wait/stop/switch/complete) or
    # {"action": "send", "message": {...}}. Both are already JSON-serializable.
    return out
