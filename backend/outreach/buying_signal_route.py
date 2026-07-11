"""Buying-signal email route — enroll high-intent voice-call prospects into the
warm ``buying_signal`` sequence instead of leaving follow-up to the cold path.

Wired-DORMANT, two independent keys (mirrors cash_engine):
  1. ``settings.outreach_buying_signal_route_enabled`` arms ENROLLMENT. The voice
     end-of-call handler calls :func:`maybe_enroll_buying_signal`; with the flag
     OFF it is a no-op, so nothing is written.
  2. SENDING is a second step. :func:`dispatch_due_buying_signal` is dry-run by
     default and is NOT wired to any scheduler, so even with enrollment armed no
     email leaves until the operator runs/wires dispatch with ``dry_run=False``.

Why this exists: a voice call that surfaces real intent (high ``intent_score`` /
``recommended_action == book_call`` / ``tier`` in high|priority) has already
established warmth. The cold ``run_campaign`` warmth gate would treat that
prospect like any other; this route fast-tracks them into a short warm cadence.

Persistence mirrors :mod:`backend.crm.needs_warm_path`: a JSON file at
``<SAMUS_ARTIFACT_ROOT>/buying_signal_enrollments.json`` written atomically.
Never raises into the caller — the end-of-call webhook must not fail on a
bookkeeping miss.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from types import SimpleNamespace
from typing import Any

from .sequences import (
    BUYING_SIGNAL_SEQUENCE,
    Enrollment,
    SeqEvent,
    dispatch_due,
    get_sequence,
)

_LOG = logging.getLogger("samus.outreach.buying_signal_route")

_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data"
BUYING_SIGNAL_SEQUENCE_ID = BUYING_SIGNAL_SEQUENCE.id  # "buying_signal"

# Actions that are NOT a buying signal even if other fields look warm.
_HARD_NO_ACTIONS = frozenset({"disqualify"})


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def is_buying_signal(lead: Any, *, intent_threshold: int = 70) -> bool:
    """True when a call's lead_summary represents a real buying signal.

    ``recommended_action == book_call`` or ``tier in (high, priority)`` qualify
    regardless of score; ``disqualify`` never qualifies; otherwise an
    ``intent_score`` at or above ``intent_threshold`` qualifies.
    """
    if lead is None:
        return False
    action = getattr(lead, "recommended_action", None)
    if action in _HARD_NO_ACTIONS:
        return False
    if action == "book_call":
        return True
    tier = getattr(lead, "tier", None)
    if tier in ("high", "priority"):
        return True
    score = getattr(lead, "intent_score", None)
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return score >= intent_threshold
    return False


# ---------------------------------------------------------------------------
# Persistence (JSON, atomic replace) — mirrors needs_warm_path's fallback store
# ---------------------------------------------------------------------------

def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _store_path() -> str:
    return os.path.join(_artifact_root(), "buying_signal_enrollments.json")


def _read() -> list[dict[str, Any]]:
    path = _store_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        _LOG.warning("buying_signal store read failed (%s): %s", path, exc)
        return []
    return data if isinstance(data, list) else []


def _write(records: list[dict[str, Any]]) -> bool:
    """Atomic-replace write. Returns False on failure (caller stays best-effort)."""
    path = _store_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".bsr_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError as exc:
        _LOG.warning("buying_signal store write failed (%s): %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Record <-> Enrollment conversion
# ---------------------------------------------------------------------------

def _enrollment_from_record(rec: dict[str, Any]) -> Enrollment:
    events = [
        SeqEvent(
            type=str(e.get("type", "")),
            day=int(e.get("day", 0)),
            step=e.get("step"),
            at=str(e.get("at", "")),
        )
        for e in (rec.get("events") or [])
        if isinstance(e, dict)
    ]
    return Enrollment(
        prospect_id=str(rec.get("prospect_id", "")),
        sequence_id=str(rec.get("sequence_id", BUYING_SIGNAL_SEQUENCE_ID)),
        started_at=str(rec.get("started_at", "")),
        status=rec.get("status", "active"),
        completed_steps=[int(s) for s in (rec.get("completed_steps") or [])],
        events=events,
    )


def _apply_enrollment_to_record(rec: dict[str, Any], enr: Enrollment) -> None:
    """Write back the mutable enrollment fields onto its stored record."""
    rec["status"] = enr.status
    rec["completed_steps"] = list(enr.completed_steps)
    rec["events"] = [
        {"type": e.type, "day": e.day, "step": e.step, "at": e.at}
        for e in enr.events
    ]


# ---------------------------------------------------------------------------
# Enrollment (called from the voice end-of-call handler)
# ---------------------------------------------------------------------------

def maybe_enroll_buying_signal(
    *,
    prospect_id: str,
    lead: Any,
    now_iso: str,
    email: str = "",
    company: str = "",
    call_id: str = "",
    source: str = "voice",
    enabled_override: bool = False,
) -> dict[str, Any]:
    """Enroll a high-intent prospect into the warm buying-signal sequence.

    Gated by ``settings.outreach_buying_signal_route_enabled`` (default OFF).
    ``enabled_override=True`` lets a caller that already owns its OWN enable
    gate (the post-call remediate loop, gated by
    ``postcall_remediate_enabled``) drive enrollment without also arming the
    cold buying-signal route — the store, sequence, and idempotency are shared.
    Idempotent: a prospect with an existing active enrollment is not re-enrolled.
    Never raises — returns a result dict describing what happened.
    """
    from backend.common.config import get_settings

    try:
        settings = get_settings()
        if not enabled_override and not getattr(
            settings, "outreach_buying_signal_route_enabled", False
        ):
            return {"enrolled": False, "reason": "route_disabled"}
        if not prospect_id:
            return {"enrolled": False, "reason": "no_prospect_id"}

        threshold = int(getattr(settings, "outreach_buying_signal_intent_threshold", 70))
        if not is_buying_signal(lead, intent_threshold=threshold):
            return {"enrolled": False, "reason": "not_a_buying_signal"}

        records = _read()
        for rec in records:
            if (
                rec.get("prospect_id") == prospect_id
                and rec.get("sequence_id") == BUYING_SIGNAL_SEQUENCE_ID
                and rec.get("status") == "active"
            ):
                return {"enrolled": False, "reason": "already_enrolled"}

        record = {
            "prospect_id": prospect_id,
            "sequence_id": BUYING_SIGNAL_SEQUENCE_ID,
            "started_at": now_iso,
            "status": "active",
            "completed_steps": [],
            "events": [],
            # Context for dispatch + the operator console.
            "email": (email or "").strip(),
            "company": company or (getattr(lead, "company", None) or ""),
            "intent_score": getattr(lead, "intent_score", None),
            "recommended_action": getattr(lead, "recommended_action", None),
            "tier": getattr(lead, "tier", None),
            "call_id": call_id,
            "enrolled_at": now_iso,
            "source": source,
        }
        records.append(record)
        if not _write(records):
            return {"enrolled": False, "reason": "persist_failed"}
        _LOG.info(
            "buying_signal enrolled prospect=%s score=%s action=%s",
            prospect_id, record["intent_score"], record["recommended_action"],
        )
        return {"enrolled": True, "prospect_id": prospect_id,
                "sequence_id": BUYING_SIGNAL_SEQUENCE_ID}
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail the webhook
        _LOG.warning("maybe_enroll_buying_signal failed prospect=%s: %s",
                     prospect_id, exc)
        return {"enrolled": False, "reason": f"error:{exc}"}


# ---------------------------------------------------------------------------
# Email-reply enrollment — text-classifier counterpart to the voice route
# ---------------------------------------------------------------------------
#
# Why this exists: the voice route fires off a structured ``lead_summary``
# produced by the Vapi end-of-call analysis. Email replies arrive as free
# text — there is no upstream classifier. So the email path runs its own
# keyword/regex scorer (``is_email_reply_buying_signal``), synthesises a
# lead-shaped object, and hands off to ``maybe_enroll_buying_signal`` so
# both paths share one store, one gating flag, one dispatch.
#
# Gating mirrors the voice path: ``OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED``
# arms enrollment; sending stays DORMANT until ``dispatch_due_buying_signal``
# is wired with ``dry_run=False`` + a composer.

# Phrase → weight. Matched as case-insensitive substrings on the reply body.
# Weights are tuned so a single strong phrase (book a call, send the quote,
# what is the price) crosses the default 70 threshold on its own; soft
# phrases need to accumulate.
_EMAIL_REPLY_POSITIVE: tuple[tuple[str, int], ...] = (
    # Strong — explicit move-to-buy / move-to-call signals
    ("book a call", 75), ("book a meeting", 75), ("schedule a call", 75),
    ("set up a call", 70), ("set up a meeting", 70), ("hop on a call", 70),
    ("get on a call", 65), ("on a call", 45), ("on a quick call", 70),
    ("send me the quote", 75), ("send the quote", 75), ("send a quote", 70),
    ("send me a quote", 75), ("what is the price", 70), ("what's the price", 70),
    ("how much", 60), ("how much does", 70), ("pricing", 50),
    ("ready to start", 80), ("ready to proceed", 80), ("let's do it", 80),
    ("let's go", 65), ("sign me up", 85), ("sign us up", 85),
    ("when can we start", 70), ("when can you start", 70),
    ("next steps", 60), ("what are the next steps", 75),
    # Medium — clear interest, needs a nudge
    ("interested", 45), ("very interested", 65), ("definitely interested", 70),
    ("sounds good", 45), ("sounds great", 50), ("looks good", 45),
    ("tell me more", 50), ("want to learn more", 55), ("learn more", 35),
    ("would like to", 35), ("we'd like to", 45), ("we want to", 50),
    ("yes please", 60), ("yes,", 30), ("go ahead", 55),
    # Soft — questions that imply intent
    ("do you offer", 35), ("can you", 25), ("would you", 25),
    ("what does it", 25), ("how does it work", 35), ("how do you", 25),
)

# A hard-no in the reply VETOES enrollment regardless of positive matches —
# parallels the voice route's ``_HARD_NO_ACTIONS`` (``disqualify``).
_EMAIL_REPLY_HARD_NO: tuple[str, ...] = (
    "not interested", "no thanks", "no thank you",
    "remove me", "unsubscribe", "stop emailing", "stop contacting",
    "do not contact", "don't contact", "please remove",
    "take me off", "opt out", "opt-out",
)


def is_email_reply_buying_signal(
    reply_text: str, *, intent_threshold: int = 70,
) -> tuple[bool, int, list[str]]:
    """Classify a raw email-reply body as a buying signal.

    Returns ``(qualifies, score_capped_at_100, hits)``. A hard-no phrase
    forces score=0 and qualifies=False even if other matches add up.
    """
    text = (reply_text or "").strip().lower()
    if len(text) < 5:
        return False, 0, []
    for phrase in _EMAIL_REPLY_HARD_NO:
        if phrase in text:
            return False, 0, [f"hard_no:{phrase}"]
    hits: list[str] = []
    score = 0
    for phrase, weight in _EMAIL_REPLY_POSITIVE:
        if phrase in text:
            score += weight
            hits.append(phrase)
    if "?" in reply_text:
        score += 5
    score = min(score, 100)
    return score >= intent_threshold, score, hits


def _synthesise_email_reply_lead(
    *, score: int, company: str = "",
) -> SimpleNamespace:
    """Build a lead-shaped object so the existing voice-path enroller can
    consume an email-reply signal unchanged."""
    if score >= 85:
        tier = "high"
    elif score >= 70:
        tier = "medium"
    else:
        tier = "low"
    return SimpleNamespace(
        intent_score=score,
        recommended_action="book_call" if score >= 80 else "follow_up",
        tier=tier,
        company=company,
    )


def maybe_enroll_buying_signal_from_email_reply(
    *,
    prospect_id: str,
    reply_text: str,
    now_iso: str,
    email: str = "",
    company: str = "",
    operator_override: bool = False,
) -> dict[str, Any]:
    """Enroll a prospect into the warm buying_signal sequence based on the
    text of an inbound email reply.

    Same gating flag and same store as the voice path. With
    ``operator_override=True`` the classifier is bypassed and the prospect
    is enrolled at a fixed high score — the operator-manual escape hatch
    used when the reply lands in a human's inbox before any automated
    inbound-parse pipeline is wired.
    """
    if operator_override:
        score = 90
        hits = ["operator_override"]
        qualifies = True
    else:
        qualifies, score, hits = is_email_reply_buying_signal(reply_text)
        if not qualifies:
            return {"enrolled": False, "reason": "not_a_buying_signal",
                    "score": score, "hits": hits}
    lead = _synthesise_email_reply_lead(score=score, company=company)
    result = maybe_enroll_buying_signal(
        prospect_id=prospect_id, lead=lead, now_iso=now_iso,
        email=email, company=company, call_id="",
        source="email_reply_operator" if operator_override else "email_reply",
    )
    result.setdefault("score", score)
    result.setdefault("hits", hits)
    return result


# ---------------------------------------------------------------------------
# Website-form enrollment — the third source alongside voice and email_reply
# ---------------------------------------------------------------------------
#
# Why this exists: hustleforge.tech's onboarding form is the site Samus owns,
# and a submission with a declared service_interest + concrete timeline/budget
# is a stronger signal than a cold prospect — the person came to us. Without
# this path, form submissions land in DynamoDB samus_onboarding_leads and
# never enter any warm cadence; every website-sourced lead has to be redriven
# through outbound, which is exactly the CODB-funding leak the operator flagged.
#
# Gating (wire-not-arm): OUTREACH_WEBSITE_FORM_WARM_ENROLL_ENABLED (default OFF).
# When enabled, the enrollment call to maybe_enroll_buying_signal uses
# ``enabled_override=True`` — the website-form flag is our own arming gate and
# does not need the voice-path route flag to also be on. Dispatch stays DORMANT
# until dispatch_due_buying_signal is wired with dry_run=False + a composer.

# Low-signal filter: a submission with no declared service_interest, or only
# "not_sure", is not a warm lead — it's a "we're browsing" ping. We record
# such submissions in DDB / audit ledger but do NOT enroll them into the warm
# cadence; the email mirror already surfaces them to the operator.
_WEBSITE_FORM_NOT_SURE_MARKER = "not_sure"


def _synthesise_website_form_lead(*, stored_lead: Any) -> SimpleNamespace:
    """Score a website onboarding lead by its declared intent signals.

    Scoring rewards the strongest self-declared signals: a concrete
    service_interest earns the base; a paying-tier budget and a soon-ish
    timeline push into the 70+ enrollment threshold. Rubric is intentionally
    coarse — the point is to route the top of the intent stack into the warm
    cadence, not to grade every field precisely.
    """
    interests = [
        s for s in (getattr(stored_lead, "service_interest", None) or [])
        if s != _WEBSITE_FORM_NOT_SURE_MARKER
    ]
    budget = getattr(stored_lead, "monthly_budget", "") or ""
    timeline = getattr(stored_lead, "timeline", "") or ""
    pain = getattr(stored_lead, "pain_points", "") or ""

    score = 0
    if interests:
        score += 60  # they told us specifically what they want
    if timeline in ("asap", "this_month"):
        score += 15
    elif timeline == "next_30_days":
        score += 5
    if budget in ("$500-$2000", "$2000-$5000", "$5000+"):
        score += 15
    elif budget == "$150-$500":
        score += 5
    if len(pain) > 80:
        score += 5  # they took the time to describe the problem
    score = min(score, 100)

    if score >= 85:
        tier = "high"
    elif score >= 70:
        tier = "medium"
    else:
        tier = "low"
    return SimpleNamespace(
        intent_score=score,
        recommended_action="book_call" if score >= 80 else "follow_up",
        tier=tier,
        company=getattr(stored_lead, "company", "") or "",
    )


def maybe_enroll_buying_signal_from_website_form(
    *,
    stored_lead: Any,
    now_iso: str,
) -> dict[str, Any]:
    """Enroll a website onboarding form submission as a warm lead.

    Never raises — best-effort like the voice / email_reply paths. Returns a
    result dict describing what happened (enrolled/skipped + reason).
    """
    from backend.common.config import get_settings

    try:
        settings = get_settings()
        if not getattr(
            settings, "outreach_website_form_warm_enroll_enabled", False,
        ):
            return {"enrolled": False, "reason": "route_disabled"}

        interests = [
            s for s in (getattr(stored_lead, "service_interest", None) or [])
            if s != _WEBSITE_FORM_NOT_SURE_MARKER
        ]
        if not interests:
            return {"enrolled": False, "reason": "no_declared_interest"}

        lead_id = getattr(stored_lead, "lead_id", "") or ""
        if not lead_id:
            return {"enrolled": False, "reason": "no_lead_id"}

        lead = _synthesise_website_form_lead(stored_lead=stored_lead)
        # Prefix the lead_id so the enrollment key never collides with a
        # future real prospect_id. When prospecting later discovers the same
        # email (via sources/prior_inbound.py), the two are correlated via
        # the stored email field on the enrollment record.
        prospect_id = f"lead_{lead_id}"
        result = maybe_enroll_buying_signal(
            prospect_id=prospect_id, lead=lead, now_iso=now_iso,
            email=getattr(stored_lead, "email", "") or "",
            company=getattr(stored_lead, "company", "") or "",
            call_id="",  # no upstream call
            source="website_form",
            # The website-form flag IS our arming gate; don't require the
            # voice-path route flag to also be on.
            enabled_override=True,
        )
        result.setdefault("intent_score", lead.intent_score)
        result.setdefault("tier", lead.tier)
        return result
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail intake
        _LOG.warning(
            "maybe_enroll_buying_signal_from_website_form failed: %s", exc,
        )
        return {"enrolled": False, "reason": f"error:{exc}"}


# ---------------------------------------------------------------------------
# Cold-list exclusion — used by prospecting/text_export to keep prospects in
# an active warm sequence off tomorrow's morning_call_list.
# ---------------------------------------------------------------------------

def active_warm_prospect_ids() -> set[str]:
    """Set of prospect_ids currently in an active buying_signal enrollment.

    Best-effort: a missing / unreadable store returns an empty set so a
    storage fault never silently drops the morning call list.
    """
    out: set[str] = set()
    for rec in _read():
        if (
            rec.get("sequence_id") == BUYING_SIGNAL_SEQUENCE_ID
            and rec.get("status") == "active"
        ):
            pid = str(rec.get("prospect_id") or "").strip()
            if pid:
                out.add(pid)
    return out


# ---------------------------------------------------------------------------
# Dispatch (wired-DORMANT — dry_run by default, no scheduler)
# ---------------------------------------------------------------------------

def dispatch_due_buying_signal(
    *, now_iso: str, dry_run: bool = True, max_send: int = 25, composer=None,
) -> list[dict[str, Any]]:
    """Plan (and, when ``dry_run=False``, send) the next due touch for every
    active buying-signal enrollment.

    DORMANT by default: ``dry_run=True`` plans without sending or mutating.

    Live send requires BOTH ``dry_run=False`` AND an explicit ``composer``. The
    touch ``brief`` is an instruction to a composer, NOT prospect-facing copy —
    sending it raw would leak internal text to the prospect. So live dispatch
    fail-closes: with no ``composer`` each due touch is skipped
    (``reason='no_composer'``). ``composer`` is ``callable(message: dict,
    record: dict) -> (subject: str, body: str)`` returning the real, send-ready
    copy; wiring it is the operator's arming step.

    An enrollment with no known email is skipped (``reason='no_email'``). Never
    raises.
    """
    seq = get_sequence(BUYING_SIGNAL_SEQUENCE_ID)
    if seq is None:  # pragma: no cover — registered at import
        return []

    results: list[dict[str, Any]] = []
    sent = 0
    records = _read()
    dirty = False

    for rec in records:
        if rec.get("sequence_id") != BUYING_SIGNAL_SEQUENCE_ID:
            continue
        enr = _enrollment_from_record(rec)
        # Plan only; we mark/send ourselves so persistence + send stay in step.
        plan = dispatch_due(seq, enr, now_iso, dry_run=True)
        action = plan.get("action")
        if action != "send":
            results.append({"prospect_id": rec.get("prospect_id"), "action": action})
            continue

        message = plan["message"]
        email = (rec.get("email") or "").strip()
        company = rec.get("company") or ""
        entry: dict[str, Any] = {
            "prospect_id": rec.get("prospect_id"),
            "action": "send",
            "template_id": message.get("template_id"),
            "subject": message.get("subject"),
            "dry_run": dry_run,
        }

        if dry_run:
            entry["planned"] = True
            results.append(entry)
            continue

        if composer is None:
            # Fail-closed: never send a touch's internal brief as if it were
            # prospect copy. Live send is gated on the operator wiring a composer.
            entry.update({"sent": False, "reason": "no_composer"})
            results.append(entry)
            continue
        if not email:
            entry.update({"sent": False, "reason": "no_email"})
            results.append(entry)
            continue
        if sent >= max_send:
            entry.update({"sent": False, "reason": "max_send_reached"})
            results.append(entry)
            continue

        try:
            from .models import OutreachMessageRequest
            from .service import send_message

            step = int(message.get("step") or 0)
            composed_subject, composed_body = composer(message, rec)
            subject = str(composed_subject or message.get("subject") or "Following up")
            body = str(composed_body or "")
            if not body.strip():
                entry.update({"sent": False, "reason": "empty_body"})
                results.append(entry)
                continue
            # Attach the vibrant buy-now flyer (warm buying-signal sends are
            # promotional — a flyer + buy button converts better than text).
            # Fail-soft: "" -> text-only, same as before.
            from .flyer import flyer_html_for
            _flyer = flyer_html_for(
                prospect_id=str(rec.get("prospect_id") or ""),
                to_email=email,
                row=rec if isinstance(rec, dict) else None,
                stake=body[:280],
            )
            req = OutreachMessageRequest(
                prospect_id=str(rec.get("prospect_id") or ""),
                channel="email",
                template_id=str(message.get("template_id") or BUYING_SIGNAL_SEQUENCE_ID),
                to=email,
                subject=subject,
                body=body,
                html_body=_flyer,
                company=company,
            )
            send_message(req)
            # Mark the touch sent on the enrollment, then persist.
            for touch in seq.touches:
                if touch.step == step:
                    from .sequences import mark_sent
                    mark_sent(enr, touch, now_iso)
                    break
            _apply_enrollment_to_record(rec, enr)
            dirty = True
            sent += 1
            entry.update({"sent": True, "step": step})
        except Exception as exc:  # noqa: BLE001 — never let one prospect break the run
            _LOG.warning("buying_signal dispatch send failed prospect=%s: %s",
                         rec.get("prospect_id"), exc)
            entry.update({"sent": False, "reason": f"error:{exc}"})
        results.append(entry)

    if dirty:
        _write(records)
    return results
