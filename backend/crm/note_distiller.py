"""Distill operator call notes into structured CRM actions.

Rule-based extraction — no LLM calls on the hot path. Pulls:
  * Email addresses → Contact records (role inferred from context)
  * Scheduling constraints → CallState.next_attempt_at
  * Product angle signals → structured_data on the Conversation
  * Action items → operator tasks

Wired-dormant: ``distill_notes()`` always returns the extraction result
but only writes to the CRM when ``SAMUS_NOTE_DISTILLER_ARMED`` is set.
The operator arms it when ready.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.crm.note_distiller")

def _is_armed() -> bool:
    return os.environ.get("SAMUS_NOTE_DISTILLER_ARMED", "").strip().lower() in (
        "1", "true", "yes",
    )

# ---- email extraction -------------------------------------------------------

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

_ROLE_HINTS = {
    "gatekeeper": "gatekeeper",
    "gate keeper": "gatekeeper",
    "poc": "point_of_contact",
    "point of contact": "point_of_contact",
    "owner": "owner",
    "admin": "admin",
    "contact": "contact",
    "manager": "manager",
    "receptionist": "receptionist",
}


def _infer_role(notes_lower: str, email: str) -> str:
    idx = notes_lower.find(email.lower())
    if idx < 0:
        return "contact"
    window = notes_lower[max(0, idx - 120):idx]
    for hint, role in _ROLE_HINTS.items():
        if hint in window:
            return role
    return "contact"


def extract_emails(notes: str) -> list[dict[str, str]]:
    raw = _EMAIL_RE.findall(notes)
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    nl = notes.lower()
    for em in raw:
        key = em.lower()
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "email": em,
            "role": _infer_role(nl, em),
        })
    return results


# ---- scheduling constraints -------------------------------------------------

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_CLOSED_UNTIL_RE = re.compile(
    r"(?:closed|unavailable|out of office|away|vacation|off)\s+"
    r"(?:until|through|thru|till|from\s+\S+\s*(?:to|through|thru|-|–))\s*"
    r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?",
    re.IGNORECASE,
)

_DATE_RANGE_RE = re.compile(
    r"(?:closed|unavailable|off)\s+"
    r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:-|–|to|through|thru)\s*"
    r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?",
    re.IGNORECASE,
)


def _resolve_date(month_str: str, day: int, ref: date | None = None) -> date | None:
    ref = ref or date.today()
    m = _MONTH_MAP.get(month_str.lower())
    if m is None:
        return None
    year = ref.year
    try:
        d = date(year, m, day)
    except ValueError:
        return None
    if d < ref - timedelta(days=60):
        d = d.replace(year=year + 1)
    return d


def extract_schedule_constraint(notes: str) -> str:
    """Return an ISO date for next_attempt_at, or empty string."""
    m = _DATE_RANGE_RE.search(notes)
    if m:
        end_date = _resolve_date(m.group(3), int(m.group(4)))
        if end_date:
            return (end_date + timedelta(days=1)).isoformat()

    m = _CLOSED_UNTIL_RE.search(notes)
    if m:
        d = _resolve_date(m.group(1), int(m.group(2)))
        if d:
            return (d + timedelta(days=1)).isoformat()

    return ""


# ---- product angle signals ---------------------------------------------------

_ANGLE_PATTERNS: list[tuple[str, str]] = [
    (r"\bseo\s+audit\b", "seo_audit"),
    (r"\bdigital\s+receptionist\b", "receptionist"),
    (r"\breceptionist\b", "receptionist"),
    (r"\bsocial\s+media\s+(?:management|automation)\b", "social_media"),
    (r"\bsecurity(?:\s+(?:report|audit|scan))?\b", "security_report"),
    (r"\bwebsite\s+(?:redesign|rebuild|revamp)\b", "website"),
    (r"\bgoogle\s+(?:ads|ppc|adwords)\b", "google_ads"),
    (r"\breputation\s+management\b", "reputation"),
]

_NEGATIVE_ANGLE_PATTERNS: list[tuple[str, str]] = [
    (r"(?:don'?t\s+pitch|no\s+need\s+for|not\s+(?:a\s+good\s+)?fit\s+for)\s+seo", "seo_audit"),
    (r"strong\s+seo", "seo_audit"),
    (r"(?:don'?t\s+pitch|no\s+need\s+for)\s+social", "social_media"),
    (r"(?:don'?t\s+pitch|no\s+need\s+for)\s+security", "security_report"),
    (r"(?:don'?t\s+pitch|already\s+has)\s+(?:a\s+)?website", "website"),
    (r"(?:don'?t\s+pitch|already\s+has)\s+receptionist", "receptionist"),
]


def extract_product_angles(notes: str) -> dict[str, Any]:
    nl = notes.lower()
    pitched: list[str] = []
    for pat, label in _ANGLE_PATTERNS:
        if re.search(pat, nl):
            pitched.append(label)
    avoid: list[str] = []
    for pat, label in _NEGATIVE_ANGLE_PATTERNS:
        if re.search(pat, nl):
            avoid.append(label)
    return {"pitch": list(dict.fromkeys(pitched)), "avoid": list(dict.fromkeys(avoid))}


# ---- action items ------------------------------------------------------------

_ACTION_PATTERNS: list[tuple[str, str]] = [
    (r"(?:follow\s*up|send|deliver|provide|offered?)\s+(?:\w+\s+)?(?:free\s+)?security\s+report", "deliver_security_report"),
    (r"(?:follow\s*up|send|deliver)\s+(?:with\s+)?(?:seo\s+audit|audit)", "deliver_seo_audit"),
    (r"seo\s+audit\b", "deliver_seo_audit"),
    (r"(?:follow\s*up|send|deliver)\s+(?:with\s+)?(?:digital\s+)?receptionist\s+flyer", "send_receptionist_flyer"),
    (r"receptionist\s+flyer", "send_receptionist_flyer"),
    (r"(?:follow\s*up|send)\s+(?:with\s+)?(?:free\s+)?(?:social\s+media)\s+(?:report|proposal)", "send_social_proposal"),
]


def extract_action_items(notes: str) -> list[str]:
    nl = notes.lower()
    items: list[str] = []
    for pat, label in _ACTION_PATTERNS:
        if re.search(pat, nl) and label not in items:
            items.append(label)
    return items


# ---- main distiller ----------------------------------------------------------

def distill_notes(
    *,
    prospect_id: str,
    company: str,
    notes: str,
    outcome: str,
) -> dict[str, Any]:
    """Extract structured intelligence from operator notes.

    Always returns the extraction result. Only writes to the CRM when
    SAMUS_NOTE_DISTILLER_ARMED is set (operator-gated).
    """
    armed = _is_armed()
    if not notes or not notes.strip():
        return {"armed": armed, "extracted": False}

    emails = extract_emails(notes)
    schedule = extract_schedule_constraint(notes)
    angles = extract_product_angles(notes)
    actions = extract_action_items(notes)

    result: dict[str, Any] = {
        "armed": armed,
        "extracted": bool(emails or schedule or angles["pitch"] or actions),
        "contacts": emails,
        "next_attempt_at": schedule,
        "product_angles": angles,
        "action_items": actions,
    }

    if not armed:
        _LOG.info(
            "note_distiller: DORMANT — extracted %d contacts, schedule=%s, "
            "angles=%s, actions=%s for %s",
            len(emails), schedule or "none",
            angles["pitch"] or "none", actions or "none", prospect_id,
        )
        return result

    # -- CRM writes (only when armed) --
    ts = iso_now()

    # 1. Create Contact records for extracted emails
    contacts_created: list[str] = []
    for em_info in emails:
        try:
            from backend.crm import service as crm_service
            existing = crm_service._existing_contact_for_email(em_info["email"])
            if existing:
                _LOG.info("note_distiller: contact %s already exists", em_info["email"])
                continue
            from backend.crm.models import Contact
            from backend.crm import persistence as p
            cid = crm_service._new_contact_id()
            contact = Contact(
                contact_id=cid,
                prospect_id=prospect_id,
                name="",
                role=em_info["role"],
                email=em_info["email"],
                source="note_distiller",
                source_ref=f"call_notes:{outcome}",
                created_at=ts,
                updated_at=ts,
            )
            ok, err = p.safe_put(p._contacts_table(), contact.model_dump())
            if ok:
                contacts_created.append(cid)
                _LOG.info("note_distiller: created contact %s (%s) for %s",
                          cid, em_info["email"], prospect_id)
            else:
                _LOG.warning("note_distiller: contact create failed: %s", err)
        except Exception as exc:
            _LOG.warning("note_distiller: contact create error: %s", exc)

    # 2. Set next_attempt_at on CallState for scheduling constraints
    if schedule:
        try:
            from backend.crm import service as crm_service
            state = crm_service.get_call_state(prospect_id)
            if state and not state.next_attempt_at:
                updated = state.model_copy(update={
                    "next_attempt_at": schedule,
                    "updated_at": ts,
                })
                crm_service.upsert_call_state(updated)
                _LOG.info("note_distiller: set next_attempt_at=%s for %s",
                          schedule, prospect_id)
        except Exception as exc:
            _LOG.warning("note_distiller: schedule update error: %s", exc)

        # ALSO route the constraint into the voice callback queue. The CRM
        # CallState write above lives in DynamoDB, which the autonomous dialer
        # does NOT read in its hot path; the callback queue is the LOCAL,
        # crash-durable store the dialer's scheduled-defer gate consults. Without
        # this, an operator's "closed until <date>" note compiled but never
        # gated the dial (the prospect got re-dialed anyway). Best-effort.
        try:
            from backend.voice.callback_queue import schedule_callback
            schedule_callback(
                prospect_id=prospect_id,
                callback_date=schedule,
                company=company or "",
                reason=f"operator note ({outcome}): scheduled for {schedule}",
            )
            _LOG.info("note_distiller: queued voice callback for %s on %s",
                      prospect_id, schedule)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("note_distiller: callback queue write error: %s", exc)

    # 3. Create operator tasks for action items
    tasks_created: list[str] = []
    for action in actions:
        try:
            from backend.crm import service as crm_service
            from backend.crm.models import CreateOperatorTaskRequest
            task_result = crm_service.create_operator_task(
                CreateOperatorTaskRequest(
                    kind="deliver",
                    title=f"{action} for {company or prospect_id}",
                    description=f"Distilled from operator call notes ({outcome})",
                    due_at=schedule or "",
                    related_entity_kind="prospect",
                    related_entity_id=prospect_id,
                    source="note_distiller",
                    source_ref=f"call_notes:{outcome}",
                )
            )
            if task_result.status == "created":
                tasks_created.append(task_result.operator_task_id)
        except Exception as exc:
            _LOG.warning("note_distiller: task create error: %s", exc)

    result["contacts_created"] = contacts_created
    result["tasks_created"] = tasks_created
    return result
