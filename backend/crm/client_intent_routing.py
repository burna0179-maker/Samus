"""Route classifier intent -> operator-task shape (kind + due_at + prefix).

The LLM classifier extracts an intent tag (see
:mod:`backend.intake.correspondence_intent`). This module maps that tag to
the operator-facing shape of the resulting task:

* which task ``kind`` to create (client_correspondence vs customer_service)
* how urgent the follow-up is (encoded as ``due_at`` — the CRM list sorts by
  ``due_at || created_at``, so urgent tasks bubble to the top of the queue
  without a new priority field)
* a short title prefix so the operator queue is scannable at a glance

DESIGN NOTES

* Pure function. No side effects, no I/O. Trivially unit-testable.
* Fail-safe on unknown intents: falls back to normal priority + generic
  client-correspondence handling. Never raises.
* Time-of-computation is injected so tests are deterministic.
* Customer-service track (kind=``customer_service``) fires ONLY for
  ``service_issue_reported`` + ``escalation_needed``. Those signal an
  active problem with an existing engagement — they belong in a separate
  operator surface from ordinary correspondence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

UrgencyLevel = Literal["urgent", "high", "normal", "low"]


@dataclass(frozen=True)
class IntentAction:
    """Downstream handling shape for one classified intent."""

    task_kind: str  # "customer_service" | "client_correspondence"
    urgency: UrgencyLevel
    due_at: str  # ISO-8601 UTC, empty string if no urgency
    title_prefix: str  # e.g. "[CS/URGENT]" or "[CLIENT/COUNTER]"
    customer_service: bool


# --- intent -> (urgency, prefix, cs) table ---------------------------------
# Only inbound intents route here. Outbound intents skip task creation
# entirely (see gmail_poller — is_outbound branch).
#
# ``customer_service=True`` opens the dedicated CS operator surface. It's
# reserved for CLEAR service-issue signals — an active problem with an
# already-engaged client. Sales-cycle intents (counter_offered, agreed_
# to_move_forward, etc.) stay on the normal client_correspondence queue.

_URGENCY_MAP: dict[str, tuple[UrgencyLevel, str, bool]] = {
    # --- URGENT / customer-service track ----------------------------------
    "escalation_needed": ("urgent", "CS/ESCALATION", True),
    "service_issue_reported": ("urgent", "CS/SERVICE", True),
    # --- HIGH — needs same-day attention ----------------------------------
    "agreed_to_move_forward": ("high", "CLIENT/AGREED", False),
    "requested_meeting": ("high", "CLIENT/MEETING", False),
    # --- NORMAL — 24h response window -------------------------------------
    "counter_offered": ("normal", "CLIENT/COUNTER", False),
    "objected_price": ("normal", "CLIENT/OBJECTION", False),
    "objected_scope": ("normal", "CLIENT/OBJECTION", False),
    "objected_timing": ("normal", "CLIENT/OBJECTION", False),
    "expressed_hesitation": ("normal", "CLIENT/HESITATION", False),
    "requested_more_info": ("normal", "CLIENT/INFO", False),
    "question_general": ("normal", "CLIENT/QUESTION", False),
    # --- LOW — acknowledge but no immediate action ------------------------
    "acknowledgment": ("low", "CLIENT/ACK", False),
    "closed_out_conversation": ("low", "CLIENT/CLOSED", False),
    # --- UNKNOWN — normal priority so nothing rots -----------------------
    "unknown": ("normal", "CLIENT", False),
}


def _due_for(urgency: UrgencyLevel, now: datetime) -> str:
    """Encode urgency as an ISO due-at offset from ``now``.

    The CRM queue sorts open tasks by ``due_at``; earlier = higher in the
    list. Offsets: urgent = now, high = +4h, normal = +24h, low = +3d.
    """
    if urgency == "urgent":
        due = now
    elif urgency == "high":
        due = now + timedelta(hours=4)
    elif urgency == "normal":
        due = now + timedelta(hours=24)
    else:  # low
        due = now + timedelta(days=3)
    # ISO 8601 with Z suffix — same format as backend.common.dates.iso_now
    return (
        due.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def route_client_intent(
    intent: str | None,
    *,
    now: datetime | None = None,
) -> IntentAction:
    """Return the operator-task shape for one classified inbound intent.

    ``intent`` may be None / empty / a tag we don't know about — all fall
    back to the ``unknown`` row (normal priority, generic prefix, no CS).
    """
    key = (intent or "unknown").strip().lower()
    if key not in _URGENCY_MAP:
        key = "unknown"
    urgency, prefix, cs = _URGENCY_MAP[key]
    ts = now or datetime.now(timezone.utc)
    return IntentAction(
        task_kind="customer_service" if cs else "client_correspondence",
        urgency=urgency,
        due_at=_due_for(urgency, ts),
        title_prefix=f"[{prefix}]",
        customer_service=cs,
    )


def all_known_intents() -> list[str]:
    """Return every intent the router recognizes (stable order)."""
    return sorted(_URGENCY_MAP.keys())


__all__ = [
    "IntentAction",
    "UrgencyLevel",
    "route_client_intent",
    "all_known_intents",
]
