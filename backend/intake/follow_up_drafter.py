"""FollowUpDrafter — the "action" pod: drafts a reply, NEVER sends it.

Given a classified inbound reply, produce a suggested response draft for the
operator (or a future separately-gated auto-send path). The draft is run through
the ComplianceGuard (Component 1) so the operator sees its CAN-SPAM verdict and
the List-Unsubscribe headers it would carry, and so a commercial follow-up
already includes the postal address + unsubscribe footer. This pod returns a
draft only; the decision to actually send stays with the operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.common import compliance_guard
from backend.common.config import get_settings
from backend.intake.reply_classifier import (
    INTENT_INTERESTED,
    INTENT_MEETING_BOOKED,
    INTENT_NOT_INTERESTED,
    INTENT_OPT_OUT,
)


@dataclass(frozen=True)
class FollowUpDraft:
    intent: str
    subject: str
    body: str
    send_recommended: bool
    note: str = ""
    compliance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "subject": self.subject,
            "body": self.body,
            "send_recommended": self.send_recommended,
            "note": self.note,
            "compliance": self.compliance,
        }


def _footer(settings) -> str:
    postal = str(getattr(settings, "sender_postal_address", "") or "").strip()
    unsub = str(getattr(settings, "unsubscribe_url", "") or "").strip()
    lines = ["", "---"]
    if postal:
        lines.append(postal)
    if unsub:
        lines.append(f"Unsubscribe: {unsub}")
    return "\n".join(lines)


def _greeting(first_name: str) -> str:
    return f"Hi {first_name}," if first_name.strip() else "Hi there,"


def draft_follow_up(
    intent: str,
    *,
    original_subject: str = "",
    from_addr: str = "",
    first_name: str = "",
    company: str = "",
) -> FollowUpDraft:
    """Produce a compliance-checked follow-up draft for a classified reply.

    Returns a draft with the ComplianceGuard verdict attached. ``opt_out`` and
    ``unknown`` produce no outbound draft (send_recommended False). Never sends.
    """
    settings = get_settings()
    subject = (
        original_subject
        if original_subject.lower().startswith("re:")
        else f"Re: {original_subject}".strip()
    )

    if intent == INTENT_OPT_OUT:
        return FollowUpDraft(
            intent=intent,
            subject="",
            body="",
            send_recommended=False,
            note="Honor opt-out — suppress the address and do not reply.",
        )

    if intent == INTENT_MEETING_BOOKED:
        body = (
            f"{_greeting(first_name)}\n\n"
            "Great — happy to find a time. Here are a couple of slots that work "
            "on my end; reply with whichever suits and I'll send a calendar "
            "invite:\n\n  • (operator: insert 2-3 time options)\n\n"
            "Looking forward to it.\n" + _footer(settings)
        )
        recommend = True
        note = "Interested in meeting — operator should insert real time options before sending."
    elif intent == INTENT_INTERESTED:
        body = (
            f"{_greeting(first_name)}\n\n"
            f"Thanks for the interest! Here's a quick overview of what we'd do "
            f"for {company or 'your business'} and what it costs:\n\n"
            "  • (operator: insert tailored scope + pricing)\n\n"
            "Happy to jump on a quick call if that's easier — just say the word.\n"
            + _footer(settings)
        )
        recommend = True
        note = "Interested — operator should tailor scope/pricing before sending."
    elif intent == INTENT_NOT_INTERESTED:
        body = (
            f"{_greeting(first_name)}\n\n"
            "Totally understand — thanks for letting me know. I'll close the "
            "loop here. If anything changes down the road, the door's open.\n" + _footer(settings)
        )
        recommend = False  # low-priority courtesy; operator decides
        note = "Soft no — a courtesy close is optional; prospect stays re-approachable later."
    else:  # unknown
        return FollowUpDraft(
            intent=intent,
            subject="",
            body="",
            send_recommended=False,
            note="Intent unclear — manual review.",
        )

    verdict = compliance_guard.evaluate(
        compliance_guard.ComplianceMessage(
            to=from_addr or "prospect@example.com",
            subject=subject or "Re: your message",
            body=body,
            kind="commercial",
        )
    )
    # A draft can only be auto-send-recommended if it is BOTH intent-appropriate
    # AND clears the compliance guard.
    return FollowUpDraft(
        intent=intent,
        subject=subject or "Re: your message",
        body=body,
        send_recommended=recommend and verdict.ok,
        note=note,
        compliance=verdict.to_dict(),
    )


__all__ = ["FollowUpDraft", "draft_follow_up"]
