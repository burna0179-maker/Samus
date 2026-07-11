"""CRM service — entity reads + intake lead -> Prospect + Contact conversion + feedback engine surface."""
from __future__ import annotations

import datetime as _dt
import logging
import os
import uuid
from typing import Any

from backend.catalog.registry import sku as catalog_sku
from backend.common import conversion_funnel, events, metrics, persistence as common_persistence
from backend.common.config import get_settings
from backend.common.dates import hours_from_now, iso_now
from backend.common.http_client import signed_post_json_sync

from . import feedback_engine, hivemind_projection, lifecycle, persistence as p
from .models import (
    AdvanceOpportunityRequest,
    AdvanceOpportunityResult,
    Artifact,
    ArtifactList,
    CallState,
    Contact,
    ContactList,
    Conversation,
    ConversationList,
    ConvertLeadRequest,
    ConvertLeadResult,
    CreateArtifactRequest,
    CreateArtifactResult,
    CreateOperatorTaskRequest,
    CreateOperatorTaskResult,
    CreateOpportunityRequest,
    CreateOpportunityResult,
    FeedbackLogRequest,
    FeedbackSnapshotResponse,
    FollowUpDue,
    FollowUpList,
    OperatorTask,
    OperatorTaskList,
    Opportunity,
    OpportunityList,
    Prospect,
    UpdateOperatorTaskRequest,
    UpdateOperatorTaskResult,
)
from .pipeline import (
    STAGE_PROBABILITIES,
    TERMINAL_STAGES,
    WON_TERMINAL_STAGES,
    validate_transition,
)
from .scoring import score_opportunity_from_lead


_LOG = logging.getLogger("samus.crm.service")
_AUDIT_PATH_DEFAULT = "/opt/samus/data/crm/crm_audit.jsonl"

# Phase 6 — default operator email for auto-created opportunities
_DEFAULT_AUTO_ASSIGNEE = os.getenv("SAMUS_CRM_AUTO_ASSIGNEE", "")

# --- strategy outcome dispatch (strategy-integration build, Unit 3) --------
# When an Opportunity closes, CRM tells the strategy workcell so its bandit
# can learn from the real deal outcome. The graded reward fed to the bandit:
# a won deal is a full reward, a lost deal is zero. Mirrors the closed_won /
# closed_lost STAGE_PROBABILITIES endpoints (1.0 / 0.0).
_STRATEGY_RECORD_OUTCOME_PATH = "/strategy/record-outcome"
_OUTCOME_WON_REWARD: float = 1.0
_OUTCOME_LOST_REWARD: float = 0.0

# EMA anomaly sensor for closed_won spike detection (security-baseline §4).
# State is in-process; advisory-only until ≥2 weeks tuned in production.
_CLOSED_WON_EMA: float = 0.0
_CLOSED_WON_EMA_ALPHA: float = 0.1  # smooth over ~10 events
_CLOSED_WON_EMA_LOCK = __import__("threading").Lock()


def _check_closed_won_anomaly(won_amount_usd: float) -> None:
    global _CLOSED_WON_EMA
    with _CLOSED_WON_EMA_LOCK:
        metrics.SAMUS_CRM_CLOSED_WON_TOTAL.inc()
        if _CLOSED_WON_EMA == 0.0:
            _CLOSED_WON_EMA = won_amount_usd
        else:
            if won_amount_usd > 3.0 * _CLOSED_WON_EMA:
                metrics.SAMUS_CRM_CLOSED_WON_ANOMALY_TOTAL.inc()
                _LOG.warning(
                    "closed_won anomaly: won_amount_usd=%.2f ema=%.2f (>3x threshold)",
                    won_amount_usd, _CLOSED_WON_EMA,
                )
            _CLOSED_WON_EMA = (
                _CLOSED_WON_EMA_ALPHA * won_amount_usd
                + (1.0 - _CLOSED_WON_EMA_ALPHA) * _CLOSED_WON_EMA
            )


def _audit_ledger() -> common_persistence.JsonlLedger:
    return common_persistence.JsonlLedger(
        os.getenv("SAMUS_CRM_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def _audit(ev: dict[str, Any]) -> None:
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("crm audit append failed: %s", exc)


# ---------------------------------------------------------------------------
# ID minting
# ---------------------------------------------------------------------------

def _new_prospect_id() -> str:
    return f"pr_{uuid.uuid4().hex[:20]}"


def _new_contact_id() -> str:
    return f"co_{uuid.uuid4().hex[:20]}"


def _new_conversation_id() -> str:
    return f"cv_{uuid.uuid4().hex[:20]}"


def _new_opportunity_id() -> str:
    return f"op_{uuid.uuid4().hex[:20]}"


def _new_operator_task_id() -> str:
    return f"ot_{uuid.uuid4().hex[:20]}"


def _new_artifact_id() -> str:
    return f"ar_{uuid.uuid4().hex[:20]}"


# ---------------------------------------------------------------------------
# Single-entity reads
# ---------------------------------------------------------------------------

def get_prospect(prospect_id: str) -> Prospect | None:
    """Fetch one Prospect by id. Returns None on 404 OR on AWS error
    (the error is logged inside ``safe_get`` so callers stay simple)."""
    item, _err = p.safe_get(p._prospects_table(), {"prospect_id": prospect_id})
    if item is None:
        return None
    try:
        return Prospect.model_validate(item)
    except Exception as exc:  # noqa: BLE001 - malformed row
        _LOG.warning("malformed prospect row %s: %s", prospect_id, exc)
        return None


def get_contact(contact_id: str) -> Contact | None:
    item, _ = p.safe_get(p._contacts_table(), {"contact_id": contact_id})
    if item is None:
        return None
    try:
        return Contact.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("malformed contact row %s: %s", contact_id, exc)
        return None


def get_conversation(conversation_id: str) -> Conversation | None:
    item, _ = p.safe_get(p._conversations_table(), {"conversation_id": conversation_id})
    if item is None:
        return None
    try:
        return Conversation.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("malformed conversation row %s: %s", conversation_id, exc)
        return None


def get_call_state(prospect_id: str) -> CallState | None:
    """Note: PK is prospect_id — one current state per prospect."""
    item, _ = p.safe_get(p._call_state_table(), {"prospect_id": prospect_id})
    if item is None:
        return None
    try:
        return CallState.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("malformed call_state row %s: %s", prospect_id, exc)
        return None


def get_opportunity(opportunity_id: str) -> Opportunity | None:
    item, _ = p.safe_get(p._opportunities_table(), {"opportunity_id": opportunity_id})
    if item is None:
        return None
    try:
        return Opportunity.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("malformed opportunity row %s: %s", opportunity_id, exc)
        return None


def get_operator_task(operator_task_id: str) -> OperatorTask | None:
    item, _ = p.safe_get(p._operator_tasks_table(), {"operator_task_id": operator_task_id})
    if item is None:
        return None
    try:
        return OperatorTask.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("malformed operator_task row %s: %s", operator_task_id, exc)
        return None


def get_artifact(artifact_id: str) -> Artifact | None:
    item, _ = p.safe_get(p._artifacts_table(), {"artifact_id": artifact_id})
    if item is None:
        return None
    try:
        return Artifact.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("malformed artifact row %s: %s", artifact_id, exc)
        return None


# ---------------------------------------------------------------------------
# Filtered scans (Phase 1: full-table scan + boto FilterExpression)
# ---------------------------------------------------------------------------

def _filter_eq(field: str, value: str) -> tuple[Any, dict[str, Any], dict[str, str]]:
    """Build a DDB FilterExpression for ``field == :v`` using attribute-name
    aliasing so reserved words / hyphenated names work."""
    return (
        f"#f = :v",
        {":v": value},
        {"#f": field},
    )


def list_contacts(*, prospect_id: str | None = None, limit: int = 50) -> ContactList:
    table = p._contacts_table()
    if prospect_id:
        fe, vals, names = _filter_eq("prospect_id", prospect_id)
        items, truncated, err = p.safe_scan(
            table, limit=limit,
            filter_expression=fe,
            expression_attribute_values=vals,
            expression_attribute_names=names,
        )
    else:
        items, truncated, err = p.safe_scan(table, limit=limit)
    rows: list[Contact] = []
    for it in items:
        try:
            rows.append(Contact.model_validate(it))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skip malformed contact: %s", exc)
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return ContactList(contacts=rows, count=len(rows),
                       scan_truncated=truncated, ddb_error=err)


def list_conversations(*, prospect_id: str | None = None, limit: int = 50) -> ConversationList:
    table = p._conversations_table()
    if prospect_id:
        fe, vals, names = _filter_eq("prospect_id", prospect_id)
        items, truncated, err = p.safe_scan(
            table, limit=limit,
            filter_expression=fe,
            expression_attribute_values=vals,
            expression_attribute_names=names,
        )
    else:
        items, truncated, err = p.safe_scan(table, limit=limit)
    rows: list[Conversation] = []
    for it in items:
        try:
            rows.append(Conversation.model_validate(it))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skip malformed conversation: %s", exc)
    rows.sort(key=lambda r: r.started_at, reverse=True)
    return ConversationList(conversations=rows, count=len(rows),
                            scan_truncated=truncated, ddb_error=err)


def list_opportunities(*, stage: str | None = None, limit: int = 50) -> OpportunityList:
    table = p._opportunities_table()
    if stage:
        fe, vals, names = _filter_eq("stage", stage)
        items, truncated, err = p.safe_scan(
            table, limit=limit,
            filter_expression=fe,
            expression_attribute_values=vals,
            expression_attribute_names=names,
        )
    else:
        items, truncated, err = p.safe_scan(table, limit=limit)
    rows: list[Opportunity] = []
    for it in items:
        try:
            rows.append(Opportunity.model_validate(it))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skip malformed opportunity: %s", exc)
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return OpportunityList(opportunities=rows, count=len(rows),
                           scan_truncated=truncated, ddb_error=err)


def list_operator_tasks(*, status: str | None = "open", limit: int = 50) -> OperatorTaskList:
    table = p._operator_tasks_table()
    if status:
        # 'status' is a DDB reserved word — must alias via #f.
        fe, vals, names = _filter_eq("status", status)
        items, truncated, err = p.safe_scan(
            table, limit=limit,
            filter_expression=fe,
            expression_attribute_values=vals,
            expression_attribute_names=names,
        )
    else:
        items, truncated, err = p.safe_scan(table, limit=limit)
    rows: list[OperatorTask] = []
    for it in items:
        try:
            rows.append(OperatorTask.model_validate(it))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skip malformed operator_task: %s", exc)
    rows.sort(key=lambda r: r.due_at or r.created_at)
    return OperatorTaskList(tasks=rows, count=len(rows),
                            scan_truncated=truncated, ddb_error=err)


def list_artifacts(*, owner_entity_id: str | None = None, limit: int = 50) -> ArtifactList:
    table = p._artifacts_table()
    if owner_entity_id:
        fe, vals, names = _filter_eq("owner_entity_id", owner_entity_id)
        items, truncated, err = p.safe_scan(
            table, limit=limit,
            filter_expression=fe,
            expression_attribute_values=vals,
            expression_attribute_names=names,
        )
    else:
        items, truncated, err = p.safe_scan(table, limit=limit)
    rows: list[Artifact] = []
    for it in items:
        try:
            rows.append(Artifact.model_validate(it))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skip malformed artifact: %s", exc)
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return ArtifactList(artifacts=rows, count=len(rows),
                        scan_truncated=truncated, ddb_error=err)


# ---------------------------------------------------------------------------
# Follow-ups due — outreach-sent prospects awaiting a follow-up call
# ---------------------------------------------------------------------------

def _days_between(earlier: str, later: str) -> int:
    """Whole days from one YYYY-MM-DD date to another; 0 if either won't parse."""
    try:
        d0 = _dt.date.fromisoformat((earlier or "")[:10])
        d1 = _dt.date.fromisoformat((later or "")[:10])
    except ValueError:
        return 0
    return max(0, (d1 - d0).days)


def _parse_iso(ts: str) -> _dt.datetime | None:
    """Parse an iso_now()-style 'YYYY-MM-DDTHH:MM:SSZ' timestamp to UTC.

    Returns None on any parse failure — callers treat that as "latency
    unknown" rather than raising. Tolerant of the trailing 'Z' suffix
    iso_now() emits and of an already-offset-aware string.
    """
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        normalised = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = _dt.datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def latency_to_resolution_sec(created_at: str, actual_close: str) -> float | None:
    """Whole seconds from an Opportunity's creation to its terminal close.

    ``created_at`` and ``actual_close`` are iso_now()-style timestamps.
    Returns ``None`` when either timestamp is missing or unparseable, or when
    the close precedes creation (a clock skew / data error — telemetry should
    not report a negative latency). Pure helper, never raises.
    """
    start = _parse_iso(created_at)
    end = _parse_iso(actual_close)
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    if delta < 0:
        return None
    return round(delta, 3)


def _opportunity_for_prospect(prospect_id: str) -> Opportunity | None:
    """Most recent Opportunity for a prospect, or None — a filtered scan."""
    if not prospect_id:
        return None
    fe, vals, names = _filter_eq("prospect_id", prospect_id)
    items, _trunc, _err = p.safe_scan(
        p._opportunities_table(), limit=25,
        filter_expression=fe,
        expression_attribute_values=vals,
        expression_attribute_names=names,
    )
    opps: list[Opportunity] = []
    for it in items:
        try:
            opps.append(Opportunity.model_validate(it))
        except Exception:  # noqa: BLE001
            continue
    if not opps:
        return None
    opps.sort(key=lambda o: o.created_at, reverse=True)
    return opps[0]


def get_opportunity_for_prospect(prospect_id: str) -> Opportunity | None:
    """Public accessor: the most recent Opportunity for a prospect, or None.

    Thin wrapper over the module-private ``_opportunity_for_prospect`` so
    cross-workcell callers (the cash_engine front-door gate) have a stable
    public contract for the prospect -> opportunity resolution the stake
    check depends on, instead of reaching into a private helper.
    """
    return _opportunity_for_prospect(prospect_id)


# Deterministic interest-signal -> catalog SKU map for the follow-up upsell.
# Each SKU lists lowercase substrings; the SKU with the most distinct corpus
# hits wins (a tie yields no suggestion — never guess). sku_ids must stay in
# sync with backend.catalog.registry.
_UPSELL_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("service_workflow_buildout", (
        "workflow", "automation", "automate", "automated", "handoff",
        "manual process", "our process", "operations", " ops ", "integrate",
        "spreadsheet", "disorganized", "scaling", "scale up", "systems",
    )),
    ("retainer_ai_receptionist", (
        "missed call", "missing call", "miss call", "answer the phone",
        "receptionist", "answering service", "voicemail", "front desk",
        "after hours", "after-hours", "booking", "appointment", "scheduling",
    )),
    ("service_seo_implementation", (
        "seo", "ranking", "rank higher", "google", "search results",
        "keyword", "visibility", "found online", "organic traffic",
    )),
    ("retainer_ai_ops_partner_entry", (
        "retainer", "ongoing support", "monthly", "managed", "long term",
        "long-term", "partner",
    )),
)


def _suggest_upsell(corpus: str) -> tuple[str, str, str]:
    """Pick a 'second opportunity' SKU from interest signals in a text corpus.

    Deterministic — counts distinct lowercase-substring hits per SKU and takes
    the clear winner. Returns ``("", "", "")`` when nothing scored or the top
    two SKUs tie (no clear signal — don't put a guessed pitch in front of the
    operator). Resolves display name + a one-line pitch from the catalog so the
    SKU registry stays the single source of truth.
    """
    text = (corpus or "").lower()
    if not text.strip():
        return "", "", ""
    best_sku = ""
    best_hits: list[str] = []
    tie = False
    for sku_id, signals in _UPSELL_SIGNALS:
        matched = [s for s in signals if s in text]
        if len(matched) > len(best_hits):
            best_sku, best_hits, tie = sku_id, matched, False
        elif matched and len(matched) == len(best_hits):
            tie = True
    if not best_sku or tie:
        return "", "", ""
    try:
        entry = catalog_sku(best_sku)
    except KeyError:
        _LOG.warning("upsell SKU %r not in catalog — skipping", best_sku)
        return "", "", ""
    price = entry.price_usd_cents / 100
    price_str = f"${price:,.0f}/mo" if entry.recurring else f"${price:,.0f}"
    signal = best_hits[0].strip()
    pitch = (f"offer {entry.display_name} ({price_str}) — "
             f'they signalled "{signal}"')
    return best_sku, entry.display_name, pitch


def _follow_up_signal(prospect_id: str) -> tuple[Conversation | None, str]:
    """For a follow-up prospect, return (latest outreach Conversation, corpus).

    The corpus is every Conversation's transcript + summary + subject plus the
    prospect's Opportunity ``next_step`` + ``name`` — the raw interest material
    :func:`_suggest_upsell` scans for a second-opportunity SKU. One
    ``list_conversations`` call serves both the join and the corpus.
    """
    convos = list_conversations(prospect_id=prospect_id, limit=50).conversations
    outreach = sorted(
        (c for c in convos if c.outcome == "outreach_sent"),
        key=lambda c: c.started_at, reverse=True,
    )
    parts: list[str] = []
    for c in convos:
        parts += [c.transcript or "", c.summary or "",
                  str((c.structured_data or {}).get("subject", ""))]
    opp = _opportunity_for_prospect(prospect_id)
    if opp:
        parts += [opp.next_step or "", opp.name or ""]
    return (outreach[0] if outreach else None), " ".join(s for s in parts if s)


def list_follow_ups_due(*, today: str, limit: int = 100) -> FollowUpList:
    """Prospects whose outreach follow-up call is due on or before ``today``.

    The 'due' signal is a CallState in the ``outreach_sent`` state whose
    ``next_attempt_at`` (a YYYY-MM-DD date) is on or before ``today``. Each is
    joined to its originating outreach Conversation: the Conversation's
    ``structured_data`` carries the company + phone the operator needs to place
    the call, because the outreach workcell only knew the prospect by id +
    email.

    A prospect drops off this list automatically once a call is logged against
    it — logging a call upserts CallState to a connected / no-answer state,
    moving it off ``outreach_sent``.

    Each FollowUpDue also carries a deterministic "second opportunity" upsell
    (:func:`_suggest_upsell`) — a catalog SKU inferred from interest signals in
    the prospect's conversations + Opportunity, all-"" when no clear signal.
    """
    fe, vals, names = _filter_eq("state", "outreach_sent")
    items, truncated, err = p.safe_scan(
        p._call_state_table(), limit=limit,
        filter_expression=fe,
        expression_attribute_values=vals,
        expression_attribute_names=names,
    )
    due: list[FollowUpDue] = []
    for it in items:
        try:
            state = CallState.model_validate(it)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skip malformed call_state: %s", exc)
            continue
        # next_attempt_at holds the scheduled follow-up DATE. Empty = unset
        # (skip); a date after today = not due yet (skip).
        scheduled = (state.next_attempt_at or "")[:10]
        if not scheduled or scheduled > today:
            continue
        convo, corpus = _follow_up_signal(state.prospect_id)
        sd: dict[str, Any] = convo.structured_data if convo else {}
        emailed_on = str(sd.get("emailed_on")
                         or (convo.started_at[:10] if convo else ""))
        upsell_sku, upsell_name, upsell_pitch = _suggest_upsell(corpus)
        due.append(FollowUpDue(
            prospect_id=state.prospect_id,
            company=str(sd.get("company") or ""),
            phone=str(sd.get("phone") or ""),
            channel=str(sd.get("channel") or (convo.channel if convo else "")),
            subject=str(sd.get("subject") or (convo.summary if convo else "")),
            emailed_on=emailed_on,
            follow_up_on=scheduled,
            days_waiting=_days_between(emailed_on, today),
            attempt_count=state.attempt_count,
            upsell_sku=upsell_sku,
            upsell_name=upsell_name,
            upsell_pitch=upsell_pitch,
        ))
    due.sort(key=lambda f: (f.follow_up_on, f.company.lower()))
    return FollowUpList(follow_ups=due, count=len(due),
                        scan_truncated=truncated, ddb_error=err)


# ---------------------------------------------------------------------------
# Lead -> Prospect + Contact conversion
# ---------------------------------------------------------------------------

def _existing_contact_for_email(email: str) -> Contact | None:
    """Lookup contact by email via filtered scan. Returns first match or None."""
    if not email:
        return None
    fe, vals, names = _filter_eq("email", email.strip().lower())
    items, _trunc, _err = p.safe_scan(
        p._contacts_table(), limit=5,
        filter_expression=fe,
        expression_attribute_values=vals,
        expression_attribute_names=names,
    )
    for it in items:
        try:
            return Contact.model_validate(it)
        except Exception:  # noqa: BLE001
            continue
    return None


def _existing_prospect_for_website(website_url: str) -> Prospect | None:
    """Lookup prospect by website via filtered scan. Returns first match."""
    if not website_url:
        return None
    fe, vals, names = _filter_eq("website_url", website_url.strip().lower())
    items, _trunc, _err = p.safe_scan(
        p._prospects_table(), limit=5,
        filter_expression=fe,
        expression_attribute_values=vals,
        expression_attribute_names=names,
    )
    for it in items:
        try:
            return Prospect.model_validate(it)
        except Exception:  # noqa: BLE001
            continue
    return None


def convert_lead_to_prospect(req: ConvertLeadRequest) -> ConvertLeadResult:
    """Read an intake lead, create (or reuse) Prospect + Contact rows.

    Idempotency:
      - if a Contact already exists with the same email, return its
        prospect_id + contact_id with ``status="existing"``
      - else if a Prospect already exists with the same website_url, attach
        a new Contact to it and return ``status="created"`` (contact-only)
      - else create both new and return ``status="created"``

    Failures (lead not found, DDB writes failing) return ``status="failed"``
    with the error string surfaced.
    """
    ts = iso_now()

    # 1. Read the source lead
    lead_item, lead_err = p.safe_get(
        p._onboarding_leads_table(), {"lead_id": req.lead_id},
    )
    if lead_err is not None:
        return ConvertLeadResult(
            status="failed", prospect_id="", contact_id="",
            lead_id=req.lead_id, ts=ts, error=lead_err,
        )
    if lead_item is None:
        return ConvertLeadResult(
            status="failed", prospect_id="", contact_id="",
            lead_id=req.lead_id, ts=ts, error="lead_not_found",
        )

    lead_email = (lead_item.get("email") or "").strip().lower()
    lead_company = (lead_item.get("company") or "").strip()
    lead_website = (lead_item.get("website_url") or "").strip().lower()
    lead_name = (lead_item.get("name") or "").strip()
    lead_pain = lead_item.get("pain_points") or ""
    lead_budget = lead_item.get("monthly_budget") or ""
    lead_services = lead_item.get("service_interest") or []
    if not isinstance(lead_services, list):
        lead_services = []

    # 2. Idempotency: existing contact by email?
    existing = _existing_contact_for_email(lead_email)
    if existing is not None:
        ev = events.build_audit_event(
            service="crm", task_id=req.lead_id, action="convert_lead",
            input_payload={"lead_id": req.lead_id, "email_tail": lead_email[-12:]},
            output_payload={"prospect_id": existing.prospect_id,
                            "contact_id": existing.contact_id,
                            "status": "existing"},
            status="completed",
        )
        _audit(ev)
        return ConvertLeadResult(
            status="existing",
            prospect_id=existing.prospect_id,
            contact_id=existing.contact_id,
            lead_id=req.lead_id, ts=ts,
        )

    # 3. Existing prospect by website? Attach new contact; else create both.
    existing_prospect = _existing_prospect_for_website(lead_website)
    if existing_prospect is not None and existing_prospect.prospect_id:
        prospect_id = existing_prospect.prospect_id
    else:
        # ProspectRecord has a fixed 32-field shape with extra="forbid"; we
        # only set the few we can infer from the intake lead. Pain points,
        # budget, and service interest stay on the source lead row (already
        # in samus_onboarding_leads) and can be re-read when needed.
        prospect_id = _new_prospect_id()
        prospect = Prospect(
            prospect_id=prospect_id,
            company_name=lead_company or lead_name or "(unknown)",
            website_url=lead_website,
        )
        ok, err = p.safe_put(p._prospects_table(), prospect.model_dump())
        if not ok:
            return ConvertLeadResult(
                status="failed", prospect_id="", contact_id="",
                lead_id=req.lead_id, ts=ts, error=err,
            )

    # 4. Create the Contact
    contact_id = _new_contact_id()
    contact = Contact(
        contact_id=contact_id,
        prospect_id=prospect_id,
        name=lead_name,
        email=lead_email,
        preferred_channel="email",
        source="intake_lead",
        source_ref=req.lead_id,
        created_at=ts,
        updated_at=ts,
    )
    ok, err = p.safe_put(p._contacts_table(), contact.model_dump())
    if not ok:
        return ConvertLeadResult(
            status="failed", prospect_id=prospect_id, contact_id="",
            lead_id=req.lead_id, ts=ts, error=err,
        )

    # 5. Audit
    ev = events.build_audit_event(
        service="crm", task_id=req.lead_id, action="convert_lead",
        input_payload={
            "lead_id": req.lead_id,
            "email_tail": lead_email[-12:],
            "company": lead_company[:64],
            "services": list(lead_services),
            "budget": lead_budget,
            "assigned_to": req.assigned_to,
        },
        output_payload={
            "prospect_id": prospect_id, "contact_id": contact_id,
            "status": "created",
        },
        status="completed",
    )
    _audit(ev)

    # Conversion-funnel telemetry — a lead just became a prospect. Best-effort:
    # a telemetry hiccup must never fail the conversion.
    conversion_funnel.record_stage(
        "prospect", entity_id=prospect_id, source="convert_lead",
    )

    # Unified business-event ledger (HOTL Tranche 1) — lead->prospect convert.
    # No direct taxonomy fit, so decision.made carries the transition in
    # metadata. Fail-soft by contract: emit_business_event never raises.
    from backend.common.business_events import DECISION_MADE, emit_business_event
    emit_business_event(
        DECISION_MADE,
        workcell="crm",
        prospect_id=prospect_id,
        metadata={
            "decision": "lead_converted_to_prospect",
            "lead_id": req.lead_id,
            "contact_id": contact_id,
        },
    )

    return ConvertLeadResult(
        status="created", prospect_id=prospect_id, contact_id=contact_id,
        lead_id=req.lead_id, ts=ts,
    )


# ---------------------------------------------------------------------------
# Conversation + CallState upserts (Phase 2 — voice end-of-call dispatch)
# ---------------------------------------------------------------------------
# These are the write side of the two read endpoints already in app.py. The
# voice workcell calls them after an end-of-call webhook to persist the
# transcript/summary as a Conversation row and refresh the per-prospect
# CallState FSM row. Idempotent: same PK overwrites cleanly.

def upsert_conversation(conv: Conversation) -> bool:
    """Persist (insert or overwrite) a Conversation row keyed by ``conversation_id``.

    Returns True on success, False on AWS error. A single audit entry is
    written either way — successful writes carry the conversation_id +
    source/source_ref + duration; failures carry the ddb_put_failed string.
    The caller is responsible for treating False as best-effort and continuing
    (no exception is raised even on DDB outage).
    """
    ts = iso_now()
    ok, err = p.safe_put(p._conversations_table(), conv.model_dump())
    ev = events.build_audit_event(
        service="crm",
        task_id=conv.conversation_id,
        action="upsert_conversation",
        input_payload={
            "conversation_id": conv.conversation_id,
            "prospect_id": conv.prospect_id,
            "channel": conv.channel,
            "status": conv.status,
            "duration_sec": conv.duration_sec,
            "source": conv.source,
            "source_ref": conv.source_ref,
        },
        output_payload={"persisted": ok, "error": err},
        status="completed" if ok else "degraded",
    )
    _audit(ev)
    if not ok:
        _LOG.warning("upsert_conversation failed for %s: %s",
                     conv.conversation_id, err)
    else:
        # Best-effort mirror of the prospect -> contact -> conversation
        # sub-graph into the local KG. No-op unless the projection flag is on
        # AND Neo4j is reachable; the DDB row above stays authoritative.
        _project_conversation_to_hivemind(conv)
    return ok


def upsert_call_state(state: CallState) -> bool:
    """Persist (insert or overwrite) a CallState row keyed by ``prospect_id``.

    One current FSM state per prospect (PK collision = overwrite). Same
    error-handling contract as :func:`upsert_conversation`: returns bool,
    never raises, always emits one audit entry.
    """
    # Stamp updated_at if the caller didn't bother — keeps the row's audit
    # trail honest without forcing every caller to format an ISO timestamp.
    if not state.updated_at:
        state = state.model_copy(update={"updated_at": iso_now()})
    ok, err = p.safe_put(p._call_state_table(), state.model_dump())
    ev = events.build_audit_event(
        service="crm",
        task_id=state.prospect_id,
        action="upsert_call_state",
        input_payload={
            "prospect_id": state.prospect_id,
            "state": state.state,
            "attempt_count": state.attempt_count,
            "last_call_id": state.last_call_id,
            "last_outcome": state.last_outcome,
        },
        output_payload={"persisted": ok, "error": err},
        status="completed" if ok else "degraded",
    )
    _audit(ev)
    if not ok:
        _LOG.warning("upsert_call_state failed for %s: %s",
                     state.prospect_id, err)
    return ok


# ---------------------------------------------------------------------------
# Opportunity create + advance (Phase 3 — pipeline FSM + deal scoring)
# ---------------------------------------------------------------------------

def create_opportunity(req: CreateOpportunityRequest) -> CreateOpportunityResult:
    """Mint an Opportunity, persist it, audit, and return the id.

    If ``intent_score`` / ``monthly_budget`` / ``service_interest`` were
    provided, the scoring helpers auto-fill tier (recorded on next_step as
    a human hint), close_probability, and deal_size_usd. Otherwise the
    new opportunity is created with the stage=``new`` baseline probability
    from STAGE_PROBABILITIES.
    """
    ts = iso_now()
    opportunity_id = _new_opportunity_id()

    # Score from lead signals if any were passed; otherwise fall back to
    # the stage=new baseline.
    if (
        req.intent_score is not None
        or req.monthly_budget
        or req.service_interest
    ):
        tier, close_probability, deal_size = score_opportunity_from_lead({
            "intent_score": req.intent_score,
            "monthly_budget": req.monthly_budget,
            "service_interest": list(req.service_interest),
        })
        tier_hint = f"tier={tier}"
    else:
        tier = "low"
        close_probability = STAGE_PROBABILITIES["new"]
        deal_size = 0.0
        tier_hint = ""

    next_step = req.next_step or tier_hint

    opportunity = Opportunity(
        opportunity_id=opportunity_id,
        prospect_id=req.prospect_id,
        contact_id=req.contact_id,
        stage="new",
        name=req.name or f"Opportunity {opportunity_id[-6:]}",
        deal_size_usd=deal_size,
        close_probability=close_probability,
        next_step=next_step,
        expected_close=req.expected_close,
        assigned_to=req.assigned_to,
        created_at=ts,
        updated_at=ts,
        # Strategy bandit attribution (Unit 3): persist whatever arm + reward
        # snapshot the request carried so the eventual close can be credited
        # back to the bandit arm. Defaults stay "" / 0 / False when the
        # request had no call-list provenance.
        industry=req.industry,
        policy_family=req.policy_family,
        seo_score=req.seo_score,
        owner_email=req.owner_email,
        social_facebook=req.social_facebook,
        social_instagram=req.social_instagram,
        # Per-prospect LLM cost (strategy-integration build, Unit 4).
        token_cost_usd=req.token_cost_usd,
        # Operator-authored stake_sentence (optional at create; outreach
        # refuses to fire while it stays empty).
        stake_sentence=req.stake_sentence,
        stake_sentence_authored_by=(
            req.stake_sentence_authored_by if req.stake_sentence else ""
        ),
        stake_sentence_authored_at=(ts if req.stake_sentence else ""),
    )

    ok, err = p.safe_put(p._opportunities_table(), opportunity.model_dump())
    if not ok:
        return CreateOpportunityResult(
            status="failed", opportunity_id="", ts=ts, error=err,
        )

    ev = events.build_audit_event(
        service="crm", task_id=opportunity_id, action="create_opportunity",
        input_payload={
            "prospect_id": req.prospect_id,
            "contact_id": req.contact_id,
            "intent_score": req.intent_score,
            "budget": req.monthly_budget,
            "services": list(req.service_interest),
            "assigned_to": req.assigned_to,
        },
        output_payload={
            "opportunity_id": opportunity_id,
            "stage": "new",
            "tier": tier,
            "close_probability": close_probability,
            "deal_size_usd": deal_size,
        },
        status="completed",
    )
    _audit(ev)

    # Conversion-funnel telemetry — a prospect just became an opportunity.
    # Best-effort: never fails the create.
    conversion_funnel.record_stage(
        "opportunity", entity_id=opportunity_id,
        source="create_opportunity", industry=req.industry,
    )

    # Unified business-event ledger (HOTL Tranche 1) — prospect->opportunity.
    # No direct taxonomy fit for "opportunity created", so decision.made
    # carries it in metadata. Fail-soft by contract: never raises.
    from backend.common.business_events import DECISION_MADE, emit_business_event
    emit_business_event(
        DECISION_MADE,
        workcell="crm",
        prospect_id=req.prospect_id,
        opportunity_id=opportunity_id,
        metadata={
            "decision": "opportunity_created",
            "stage": "new",
            "tier": tier,
            "deal_size_usd": deal_size,
        },
    )

    # Hivemind projection (CRM Phase-2) — mirror the new deal into the local
    # knowledge graph. Flag-gated default-OFF + best-effort: a no-op when the
    # flag is off or Neo4j is down, and never raises.
    _project_opportunity_to_hivemind(opportunity)

    return CreateOpportunityResult(
        status="created", opportunity_id=opportunity_id, ts=ts,
    )


def set_opportunity_stake(
    *,
    opportunity_id: str,
    stake_sentence: str,
    authored_by: str,
    authored_at: str,
) -> Opportunity | None:
    """Persist a guard-validated stake_sentence onto an existing Opportunity.

    Caller is expected to have already run the guard + dedup + budget gates
    (see :mod:`backend.crm.stake_opportunity` for the canonical pipeline).
    The Opportunity model itself revalidates the sentence on construct, so a
    bad string still raises here — defence in depth.
    """
    opp = get_opportunity(opportunity_id)
    if opp is None:
        return None
    updated = opp.model_copy(update={
        "stake_sentence": stake_sentence,
        "stake_sentence_authored_by": authored_by,
        "stake_sentence_authored_at": authored_at,
        "updated_at": iso_now(),
    })
    ok, err = p.safe_put(p._opportunities_table(), updated.model_dump())
    if not ok:
        _LOG.warning("set_opportunity_stake persist failed opp=%s err=%s",
                     opportunity_id, err)
        return None
    _audit(events.build_audit_event(
        service="crm", task_id=opportunity_id, action="set_opportunity_stake",
        input_payload={"authored_by": authored_by},
        output_payload={"opportunity_id": opportunity_id, "ts": updated.updated_at},
        status="completed",
    ))
    return updated


def list_opportunities_pending_stake(limit: int = 50) -> list[Opportunity]:
    """Opportunities whose stake_sentence is empty/null, oldest first.

    Surfaces the work queue Alex sees on the operator console: deals waiting
    on the one line he needs to type before they can move into outreach.
    """
    result = list_opportunities(limit=max(1, min(500, int(limit) * 10)))
    pending = [
        o for o in result.opportunities
        if not (o.stake_sentence or "").strip()
    ]
    pending.sort(key=lambda o: o.created_at or "")
    return pending[:limit]


def advance_opportunity(req: AdvanceOpportunityRequest) -> AdvanceOpportunityResult:
    """Read, validate transition, mutate stage + terminal fields, persist, audit.

    Terminal-state side effects:
      - ``closed_won``  -> sets actual_close + won_amount_usd
      - ``closed_lost`` -> sets actual_close + lost_reason

    Invalid transition / not-found / DDB-fail each map to a distinct
    response ``status`` so the worker contract can branch cleanly.
    """
    ts = iso_now()

    # 1. Read current row
    item, get_err = p.safe_get(
        p._opportunities_table(), {"opportunity_id": req.opportunity_id},
    )
    if get_err is not None:
        return AdvanceOpportunityResult(
            status="failed", opportunity_id=req.opportunity_id,
            ts=ts, error=get_err,
        )
    if item is None:
        return AdvanceOpportunityResult(
            status="not_found", opportunity_id=req.opportunity_id,
            ts=ts, error="opportunity_not_found",
        )

    try:
        current = Opportunity.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        return AdvanceOpportunityResult(
            status="failed", opportunity_id=req.opportunity_id,
            ts=ts, error=f"malformed_row: {exc}",
        )

    prior_stage = current.stage

    # 2. FSM gate
    allowed, reason = validate_transition(prior_stage, req.target_stage)
    if not allowed:
        ev = events.build_audit_event(
            service="crm", task_id=req.opportunity_id,
            action="advance_opportunity",
            input_payload={
                "opportunity_id": req.opportunity_id,
                "target_stage": req.target_stage,
            },
            output_payload={
                "status": "invalid_transition",
                "prior_stage": prior_stage,
                "reason": reason,
            },
            status="rejected",
        )
        _audit(ev)
        return AdvanceOpportunityResult(
            status="invalid_transition",
            opportunity_id=req.opportunity_id,
            prior_stage=prior_stage,
            new_stage=prior_stage,
            ts=ts,
            error=reason,
        )

    # 2b. Hard closed-won amount cap (redteam FIN-10) — fail CLOSED.
    # A crafted-large won_amount_usd (Stripe amount_total, or a forged SQS
    # close_opportunity_from_payment message) must NOT auto-advance the
    # opportunity to a won-close with an inflated amount. This is the HARD
    # control (blocks the state write); the EMA sensor below is the soft
    # advisory signal. The opportunity is left in its current stage and a
    # loud anomaly evidence event is emitted; the caller (Stripe webhook /
    # SQS worker) reads the structured blocked status and surfaces it.
    if req.target_stage in ("closed_won", "closed_won_retainer"):
        # Effective close amount, mirroring the per-stage side effects in
        # step 3: closed_won takes req.won_amount_usd or current.deal_size_usd;
        # closed_won_retainer carries the opportunity's existing won amount.
        if req.target_stage == "closed_won":
            effective_amount = float(
                req.won_amount_usd or current.deal_size_usd or 0.0
            )
        else:
            effective_amount = float(
                req.won_amount_usd or current.won_amount_usd or 0.0
            )
        cap = float(get_settings().crm_max_close_amount_usd or 0.0)
        if cap > 0 and effective_amount > cap:
            metrics.SAMUS_CRM_CLOSED_WON_BLOCKED_TOTAL.inc()
            _LOG.error(
                "crm.closed_won_blocked_over_cap: opportunity=%s amount=%.2f "
                "cap=%.2f target_stage=%s source=%s — close REFUSED, "
                "opportunity left in stage %s",
                req.opportunity_id, effective_amount, cap, req.target_stage,
                (req.notes or "")[:120], prior_stage,
            )
            ev = events.build_audit_event(
                service="crm", task_id=req.opportunity_id,
                action="advance_opportunity",
                input_payload={
                    "opportunity_id": req.opportunity_id,
                    "target_stage": req.target_stage,
                    "won_amount_usd": effective_amount,
                    "source": (req.notes or "")[:120],
                },
                output_payload={
                    "status": "blocked_over_cap",
                    "prior_stage": prior_stage,
                    "amount": effective_amount,
                    "cap": cap,
                    "reason": "closed_won_blocked_over_cap",
                },
                status="rejected",
            )
            _audit(ev)
            return AdvanceOpportunityResult(
                status="blocked_over_cap",
                opportunity_id=req.opportunity_id,
                prior_stage=prior_stage,
                new_stage=prior_stage,
                ts=ts,
                error=(
                    f"closed_won_blocked_over_cap: amount={effective_amount:.2f} "
                    f"exceeds cap={cap:.2f}"
                ),
            )

    # 3. Apply mutation + terminal side effects
    updated = current.model_copy(update={
        "stage": req.target_stage,
        "close_probability": STAGE_PROBABILITIES[req.target_stage],
        "updated_at": ts,
    })

    if req.target_stage in TERMINAL_STAGES:
        updated = updated.model_copy(update={"actual_close": ts})
    if req.target_stage == "closed_won":
        updated = updated.model_copy(update={
            "won_amount_usd": req.won_amount_usd or current.deal_size_usd,
        })
    if req.target_stage == "closed_lost":
        updated = updated.model_copy(update={
            "lost_reason": req.lost_reason or "unspecified",
        })

    ok, err = p.safe_put(p._opportunities_table(), updated.model_dump())
    if not ok:
        return AdvanceOpportunityResult(
            status="failed", opportunity_id=req.opportunity_id,
            prior_stage=prior_stage, new_stage=prior_stage,
            ts=ts, error=err,
        )

    # Telemetry — for a terminal-stage close, record the deal's
    # resolution latency (created_at -> actual_close). None on a non-terminal
    # advance or when either timestamp is unparseable.
    resolution_latency = (
        latency_to_resolution_sec(updated.created_at, updated.actual_close)
        if req.target_stage in TERMINAL_STAGES
        else None
    )

    ev = events.build_audit_event(
        service="crm", task_id=req.opportunity_id,
        action="advance_opportunity",
        input_payload={
            "opportunity_id": req.opportunity_id,
            "target_stage": req.target_stage,
            "notes": (req.notes or "")[:200],
        },
        output_payload={
            "status": "advanced",
            "prior_stage": prior_stage,
            "new_stage": req.target_stage,
            "close_probability": updated.close_probability,
            "won_amount_usd": updated.won_amount_usd,
            "lost_reason": updated.lost_reason,
            "latency_to_resolution_sec": resolution_latency,
        },
        status="completed",
    )
    _audit(ev)

    # Conversion-funnel telemetry — record the deal entering a funnel stage we
    # track for leak analysis (proposal / closed_won / closed_lost). The
    # closed_won_retainer terminal counts as a won close. Best-effort: a
    # telemetry hiccup must never undo the stage advance above.
    _funnel_stage = {
        "proposal": "proposal",
        "closed_won": "closed_won",
        "closed_won_retainer": "closed_won",
        "closed_lost": "closed_lost",
    }.get(req.target_stage)
    if _funnel_stage is not None:
        conversion_funnel.record_stage(
            _funnel_stage, entity_id=req.opportunity_id,
            source="advance_opportunity", industry=updated.industry,
        )

    # Unified business-event ledger (HOTL Tranche 1) — every stage advance.
    # `proposal` maps directly to proposal.sent (the proposal workcell is
    # dispatched below); every other stage change lands as decision.made with
    # the transition in metadata. Fail-soft by contract: never raises.
    from backend.common.business_events import (
        DECISION_MADE,
        PROPOSAL_SENT,
        emit_business_event,
    )
    emit_business_event(
        PROPOSAL_SENT if req.target_stage == "proposal" else DECISION_MADE,
        workcell="crm",
        prospect_id=(updated.prospect_id or None),
        opportunity_id=req.opportunity_id,
        revenue_usd=(
            updated.won_amount_usd
            if req.target_stage in ("closed_won", "closed_won_retainer")
            else None
        ),
        metadata={
            "decision": "opportunity_stage_advanced",
            "prior_stage": prior_stage,
            "new_stage": req.target_stage,
        },
    )

    if req.target_stage in ("closed_won", "closed_won_retainer"):
        try:
            _check_closed_won_anomaly(updated.won_amount_usd or 0.0)
        except Exception:
            pass  # anomaly sensor must never block a legitimate close

    # A terminal stage means the deal is decided — tell the strategy workcell
    # so its bandit can learn from the real outcome. Best-effort: a strategy
    # outage must never undo the opportunity write above.
    if req.target_stage in TERMINAL_STAGES:
        closed = get_opportunity(req.opportunity_id)
        if closed is not None:
            _dispatch_outcome_to_strategy(closed)

    # A non-terminal advance into the `proposal` stage is the operator
    # signalling they're ready to put an offer in front of this deal — fire
    # the proposal workcell so a draft proposal is waiting when they sit down
    # to review it. Best-effort: a proposal-workcell outage must never undo
    # the stage advance above.
    if req.target_stage == "proposal":
        _dispatch_intake_to_proposal(updated)

    # Hivemind projection (CRM Phase-2) — refresh the deal's pipeline stage in
    # the local knowledge graph after every successful advance. Flag-gated
    # default-OFF + best-effort: no-op when the flag is off or Neo4j is down,
    # and never raises.
    _project_opportunity_to_hivemind(updated)

    return AdvanceOpportunityResult(
        status="advanced",
        opportunity_id=req.opportunity_id,
        prior_stage=prior_stage,
        new_stage=req.target_stage,
        ts=ts,
        latency_to_resolution_sec=resolution_latency,
    )


# ---------------------------------------------------------------------------
# Strategy outcome dispatch — the "learn" half of the decide->learn loop
# ---------------------------------------------------------------------------
# When an Opportunity transitions to a terminal stage (closed_won / closed_lost)
# the deal outcome is known. This is the single point where CRM dispatches that
# outcome to the strategy workcell, so the hierarchical bandit that picked the
# prospect's policy_family arm at discovery time gets credit (or debit) for the
# real conversion. Every terminal-transition path in this module routes through
# this one helper so there is exactly one dispatch site.
#
# Best-effort by design — mirrors outreach.service._dispatch_outreach_to_crm:
# a missing strategy URL / shared HMAC key short-circuits cleanly, and any
# network / strategy failure is logged and swallowed. The opportunity write
# has already committed; a learn-step miss must not fail it.

def _dispatch_outcome_to_strategy(opp: Opportunity) -> None:
    """Best-effort: tell the strategy workcell a deal closed so its bandit learns.

    Fires only for terminal-stage opportunities — callers gate on
    ``TERMINAL_STAGES`` before calling. Grades the outcome (closed_won -> 1.0,
    closed_lost -> 0.0) and forwards the bandit arm (``industry`` +
    ``policy_family``) plus the reward-signal snapshot the Opportunity captured
    at creation. A pre-this-feature Opportunity with no ``industry`` /
    ``policy_family`` is still dispatched — strategy falls back to its legacy
    pattern-weight path and skips the bandit (see strategy.service.record_outcome).
    """
    settings = get_settings()
    strategy_url = settings.gateway_urls.get("strategy")
    if not strategy_url or not settings.shared_hmac_key:
        _LOG.debug("strategy outcome dispatch skipped: strategy_url / shared_hmac_key unset")
        return

    if opp.stage in WON_TERMINAL_STAGES:
        # Both closed_won and closed_won_retainer are wins (the difference is
        # revenue shape, not whether the deal converted) — grade either as a
        # full reward so the bandit credits the arm that picked this prospect.
        graded_outcome = _OUTCOME_WON_REWARD
        won = True
    else:  # closed_lost — the only non-won terminal stage
        graded_outcome = _OUTCOME_LOST_REWARD
        won = False

    payload = {
        "prospect_id": opp.prospect_id,
        "won": won,                       # back-compat with the legacy contract
        "industry": opp.industry,
        "policy_family": opp.policy_family,
        "outcome": graded_outcome,
        "seo_score": opp.seo_score,
        "owner_email": opp.owner_email,
        "social_facebook": opp.social_facebook,
        "social_instagram": opp.social_instagram,
        # Per-prospect LLM cost captured during discovery (Unit 4) — feeds
        # RewardSignal.token_cost_usd so reward density can tell a cheap win
        # from an expensive one.
        "token_cost_usd": opp.token_cost_usd,
    }
    try:
        resp = signed_post_json_sync(
            strategy_url, _STRATEGY_RECORD_OUTCOME_PATH, payload, retries=2,
        )
        if resp.status_code >= 400:
            _LOG.warning(
                "strategy outcome dispatch %s -> HTTP %s",
                _STRATEGY_RECORD_OUTCOME_PATH, resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort learn-step bookkeeping
        _LOG.warning(
            "strategy outcome dispatch failed opportunity=%s: %s",
            opp.opportunity_id, exc,
        )


# ---------------------------------------------------------------------------
# Proposal draft-ahead dispatch — the inbound deal funnel's `proposal` stage
# ---------------------------------------------------------------------------
# When an Opportunity advances into the non-terminal `proposal` stage the
# operator is ready to put an offer in front of the deal. This is the single
# point where CRM asks the proposal workcell to auto-draft a proposal, so a
# deliverable skeleton is waiting in the artifact list by review time. Mirrors
# _dispatch_outcome_to_strategy's best-effort contract and proposal.service.
# _dispatch_artifact_to_crm's gateway-dispatch shape.

def _dispatch_intake_to_proposal(opp: Opportunity) -> None:
    """Best-effort: an Opportunity entered the ``proposal`` stage — ask the
    proposal workcell to auto-draft a proposal for it.

    Fires only from the ``proposal``-stage transition in advance_opportunity.
    Dispatches via the gateway's ``POST /dispatch/proposal`` so the gateway
    routes to the ``samus-proposal`` SQS queue — proposal generation is a
    content step that must not block the operator's advance_opportunity call.
    The proposal workcell is zero-LLM / deterministic, so auto-firing carries
    no token cost (unlike the prospecting callsheet, which is opt-in).

    The Opportunity carries the deal name + the operator's next-step note but
    not a structured automation spec, so the dispatched ``OnboardingIntake``
    has empty want-lists: proposal compiles an empty workflow, validates it as
    ``needs_review``, and registers that skeleton back as a CRM artifact for
    the operator to flesh out. Best-effort by design — a missing gateway URL /
    shared HMAC key short-circuits, and any network / proposal failure is
    logged and swallowed; the stage advance has already committed.
    """
    settings = get_settings()
    gateway_url = settings.gateway_urls.get("gateway")
    if not gateway_url or not settings.shared_hmac_key:
        _LOG.debug(
            "proposal intake dispatch skipped: gateway_url / shared_hmac_key unset",
        )
        return

    task_id = f"proposal-{opp.opportunity_id}"
    envelope = {
        "task_id": task_id,
        "payload": {
            "task_id": task_id,
            "intake": {
                "client_name": (
                    opp.name or f"Opportunity {opp.opportunity_id[-6:]}"
                ),
                "business_goal": (
                    opp.next_step
                    or "Automation scoping — operator to detail triggers/actions."
                ),
                "triggers_wanted": [],
                "actions_wanted": [],
                "notifications_wanted": [],
                "tools_available": [],
                "budget_usd": opp.deal_size_usd or None,
            },
            "opportunity_id": opp.opportunity_id,
            "prospect_id": opp.prospect_id,
        },
        "metadata": {"action": "generate_proposal"},
    }
    try:
        resp = signed_post_json_sync(
            gateway_url, "/dispatch/proposal", envelope, retries=2,
        )
        if resp.status_code >= 400:
            _LOG.warning(
                "proposal intake dispatch /dispatch/proposal -> HTTP %s",
                resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort draft-ahead bookkeeping
        _LOG.warning(
            "proposal intake dispatch failed opportunity=%s: %s",
            opp.opportunity_id, exc,
        )


# ---------------------------------------------------------------------------
# Hivemind projection — mirror the deal sub-graph into the local Neo4j KG
# ---------------------------------------------------------------------------
# CRM Phase-2 (code-half). After a successful opportunity create/advance OR a
# conversation upsert, mirror the prospect -> contact -> {opportunity,
# conversation} sub-graph into the local knowledge graph via
# backend.crm.hivemind_projection. Flag-gated default-ON
# (SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED=false to disable) + local-default: a
# no-op when the flag is off OR when Neo4j is unreachable. Best-effort by design
# — the DDB write is already authoritative, so a projection miss must never fail
# or block the mutation. These are the two projection dispatch sites.

def _project_opportunity_to_hivemind(opp: Opportunity) -> None:
    """Best-effort: mirror an Opportunity's sub-graph into the local KG.

    Fast-paths out before any I/O when the projection flag is off, so the
    default (off) path adds nothing but a settings read. When armed, enriches
    the projection with the prospect + contact rows (cheap single-key gets)
    so the graph nodes carry company / contact detail, then delegates the
    schema-validated writes to :func:`hivemind_projection.project_opportunity`,
    which itself no-ops cleanly when Neo4j is down. Never raises.
    """
    try:
        if not hivemind_projection.projection_enabled():
            return
        # Short-circuit BEFORE the enrichment gets when the graph is down (the
        # common local-default case, e.g. no Neo4j in CI): no point paying two
        # DDB reads only for the projection to no-op on an unavailable client.
        gc = hivemind_projection.get_client()
        if not getattr(gc, "available", False):
            return
        prospect = get_prospect(opp.prospect_id) if opp.prospect_id else None
        contact = get_contact(opp.contact_id) if opp.contact_id else None
        hivemind_projection.project_opportunity(
            opp, prospect=prospect, contact=contact, client=gc,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort mirror, never raise
        _LOG.warning(
            "hivemind projection dispatch failed opportunity=%s: %s",
            opp.opportunity_id, exc,
        )


def _project_conversation_to_hivemind(conv: Conversation) -> None:
    """Best-effort: mirror a Conversation's sub-graph into the local KG.

    Same contract as :func:`_project_opportunity_to_hivemind`: fast-paths out
    before any I/O when the projection flag is off, enriches with the prospect
    + contact rows (cheap single-key gets) when armed so the graph nodes carry
    company / contact detail, then delegates the schema-validated writes to
    :func:`hivemind_projection.project_conversation`, which no-ops cleanly when
    Neo4j is down. Never raises.
    """
    try:
        if not hivemind_projection.projection_enabled():
            return
        gc = hivemind_projection.get_client()
        if not getattr(gc, "available", False):
            return
        prospect = get_prospect(conv.prospect_id) if conv.prospect_id else None
        contact = get_contact(conv.contact_id) if conv.contact_id else None
        hivemind_projection.project_conversation(
            conv, prospect=prospect, contact=contact, client=gc,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort mirror, never raise
        _LOG.warning(
            "hivemind projection dispatch failed conversation=%s: %s",
            conv.conversation_id, exc,
        )


# ---------------------------------------------------------------------------
# Operator tasks — create + update + lifecycle auto-generators (Phase 4)
# ---------------------------------------------------------------------------

# Allowed status transitions. Mirrors a tiny FSM:
#
#   open --> in_progress --> done
#                       \--> skipped
#   open --> done            (operator can close out without claiming)
#   open --> skipped
#
# Done / skipped are terminal — no re-opening (operators create a fresh task
# if the work needs redoing, preserving the historical close record).
_TASK_TRANSITIONS: dict[str, set[str]] = {
    "open":        {"in_progress", "done", "skipped"},
    "in_progress": {"done", "skipped"},
    "done":        set(),
    "skipped":     set(),
}
_TERMINAL_TASK_STATUSES: set[str] = {"done", "skipped"}


def _validate_task_transition(
    current: str, target: str,
) -> tuple[bool, str | None]:
    """Return ``(allowed, reason)`` for an operator-task status transition."""
    if current == target:
        return False, "noop_same_status"
    if current in _TERMINAL_TASK_STATUSES:
        return False, f"terminal_status_{current}"
    if target not in {"open", "in_progress", "done", "skipped"}:
        return False, f"unknown_target_{target}"
    if target not in _TASK_TRANSITIONS.get(current, set()):
        return False, f"illegal_transition_{current}_to_{target}"
    return True, None


def create_operator_task(
    req: CreateOperatorTaskRequest,
) -> CreateOperatorTaskResult:
    """Mint an OperatorTask, persist it, audit, and return the id."""
    ts = iso_now()
    operator_task_id = _new_operator_task_id()

    task = OperatorTask(
        operator_task_id=operator_task_id,
        kind=req.kind,
        title=req.title,
        description=req.description,
        assignee=req.assignee,
        status="open",
        due_at=req.due_at,
        related_entity_kind=req.related_entity_kind,
        related_entity_id=req.related_entity_id,
        source=req.source,
        source_ref=req.source_ref,
        created_at=ts,
        updated_at=ts,
    )

    ok, err = p.safe_put(p._operator_tasks_table(), task.model_dump())
    if not ok:
        return CreateOperatorTaskResult(
            status="failed", operator_task_id="", ts=ts, error=err,
        )

    ev = events.build_audit_event(
        service="crm", task_id=operator_task_id, action="create_task",
        input_payload={
            "kind": req.kind,
            "title": req.title[:120],
            "assignee": req.assignee,
            "source": req.source,
            "related_entity_kind": req.related_entity_kind,
            "related_entity_id": req.related_entity_id,
        },
        output_payload={
            "operator_task_id": operator_task_id,
            "status": "open",
        },
        status="completed",
    )
    _audit(ev)

    return CreateOperatorTaskResult(
        status="created", operator_task_id=operator_task_id, ts=ts,
    )


def update_operator_task(
    req: UpdateOperatorTaskRequest,
) -> UpdateOperatorTaskResult:
    """Read existing task, validate the status transition, persist + audit.

    Terminal-status side effects:
      - ``done`` / ``skipped`` -> auto-fills ``completed_at`` if the caller
        didn't supply one (defaults to ``ts``).

    ``notes``, if non-empty, is appended to the task's description (so the
    audit ledger still captures it via input_payload and the operator can
    read it via the standard GET /crm/operator-tasks/{id}).
    """
    ts = iso_now()

    item, get_err = p.safe_get(
        p._operator_tasks_table(),
        {"operator_task_id": req.operator_task_id},
    )
    if get_err is not None:
        return UpdateOperatorTaskResult(
            status="failed", operator_task_id=req.operator_task_id,
            ts=ts, error=get_err,
        )
    if item is None:
        return UpdateOperatorTaskResult(
            status="not_found", operator_task_id=req.operator_task_id,
            ts=ts, error="operator_task_not_found",
        )

    try:
        current = OperatorTask.model_validate(item)
    except Exception as exc:  # noqa: BLE001
        return UpdateOperatorTaskResult(
            status="failed", operator_task_id=req.operator_task_id,
            ts=ts, error=f"malformed_row: {exc}",
        )

    prior_status = current.status

    allowed, reason = _validate_task_transition(prior_status, req.status)
    if not allowed:
        ev = events.build_audit_event(
            service="crm", task_id=req.operator_task_id,
            action="update_task",
            input_payload={
                "operator_task_id": req.operator_task_id,
                "target_status": req.status,
            },
            output_payload={
                "status": "invalid_transition",
                "prior_status": prior_status,
                "reason": reason,
            },
            status="rejected",
        )
        _audit(ev)
        return UpdateOperatorTaskResult(
            status="invalid_transition",
            operator_task_id=req.operator_task_id,
            prior_status=prior_status,
            new_status=prior_status,
            ts=ts,
            error=reason,
        )

    update_fields: dict[str, Any] = {
        "status": req.status,
        "updated_at": ts,
    }
    if req.status in _TERMINAL_TASK_STATUSES:
        update_fields["completed_at"] = req.completed_at or ts
    if req.notes:
        existing = current.description.rstrip()
        sep = "\n\n" if existing else ""
        update_fields["description"] = f"{existing}{sep}[{ts}] {req.notes}"

    updated = current.model_copy(update=update_fields)

    ok, err = p.safe_put(p._operator_tasks_table(), updated.model_dump())
    if not ok:
        return UpdateOperatorTaskResult(
            status="failed", operator_task_id=req.operator_task_id,
            prior_status=prior_status, new_status=prior_status,
            ts=ts, error=err,
        )

    ev = events.build_audit_event(
        service="crm", task_id=req.operator_task_id, action="update_task",
        input_payload={
            "operator_task_id": req.operator_task_id,
            "target_status": req.status,
            "notes": (req.notes or "")[:200],
        },
        output_payload={
            "status": "updated",
            "prior_status": prior_status,
            "new_status": req.status,
            "completed_at": updated.completed_at,
        },
        status="completed",
    )
    _audit(ev)

    return UpdateOperatorTaskResult(
        status="updated",
        operator_task_id=req.operator_task_id,
        prior_status=prior_status,
        new_status=req.status,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# Lifecycle auto-generators — pure helpers (no I/O, no DDB)
# ---------------------------------------------------------------------------
# Phase 4 ships the helpers; wiring them into intake/finance triggers is
# the next phase. Callers receive a plain dict that can be passed straight
# into ``create_operator_task`` (via CreateOperatorTaskRequest) without any
# field-shape gymnastics.

def generate_task_from_lead(
    lead_id: str, name: str, email: str, company: str,
) -> dict[str, Any]:
    """Build an operator-task dict for a freshly arrived intake lead.

    Fires from the intake-lead lifecycle trigger (out of scope for Phase 4
    — see follow-ups). Caller persists via ``create_operator_task``.

    Shape matches :class:`CreateOperatorTaskRequest` field names so the
    caller can do ``CreateOperatorTaskRequest(**generate_task_from_lead(...))``.
    """
    company = (company or "").strip() or "(unknown company)"
    name = (name or "").strip() or "(no name)"
    email = (email or "").strip()
    return {
        "kind": "review",
        "title": f"Review new onboarding lead: {company}",
        "description": (
            f"New lead from intake form.\n"
            f"Contact: {name} <{email}>\n"
            f"Company: {company}\n"
            f"Lead ID: {lead_id}"
        ),
        "assignee": "",
        "due_at": hours_from_now(24),
        "related_entity_kind": "lead",
        "related_entity_id": lead_id,
        "source": "intake_auto",
        "source_ref": lead_id,
    }


def generate_task_for_stalled_opportunity(
    opportunity_id: str, prospect_id: str, days_in_stage: int,
) -> dict[str, Any]:
    """Build an operator-task dict for an opportunity that's gone stale.

    Fires from a periodic sweep (out of scope for Phase 4). Triggered when
    an opportunity sits in a non-terminal stage past a configured age cap.
    """
    return {
        "kind": "follow_up",
        "title": f"Follow up on stalled opportunity {opportunity_id}",
        "description": (
            f"Opportunity {opportunity_id} (prospect {prospect_id}) has "
            f"been in its current stage for {int(days_in_stage)} days "
            f"with no advance. Re-engage or close it out."
        ),
        "assignee": "",
        "due_at": hours_from_now(24),
        "related_entity_kind": "opportunity",
        "related_entity_id": opportunity_id,
        "source": "opportunity_stalled",
        "source_ref": opportunity_id,
    }


# ---------------------------------------------------------------------------
# Artifact registration + close-the-loop helpers (Phase 5)
# ---------------------------------------------------------------------------
# Producer workcells (seo, proposal, fulfillment, contract) call
# ``create_artifact`` via signed_post_json so the operator can later pull
# "all deliverables for opportunity X" from one place. The finance Stripe
# webhook calls ``find_opportunity_for_email`` + ``close_opportunity_from_payment``
# to advance an open opportunity to ``closed_won`` when payment lands.

def create_artifact(req: CreateArtifactRequest) -> CreateArtifactResult:
    """Mint an Artifact row, persist it, audit, and return the id."""
    ts = iso_now()
    artifact_id = _new_artifact_id()

    artifact = Artifact(
        artifact_id=artifact_id,
        kind=req.kind,
        owner_entity_kind=req.owner_entity_kind,
        owner_entity_id=req.owner_entity_id,
        title=req.title,
        storage_url=req.storage_url,
        inline_data=req.inline_data,
        mime_type=req.mime_type,
        bytes=req.bytes,
        source=req.source,
        created_at=ts,
        created_by=req.created_by,
    )

    ok, err = p.safe_put(p._artifacts_table(), artifact.model_dump())
    if not ok:
        return CreateArtifactResult(
            status="failed", artifact_id="", ts=ts, error=err,
        )

    ev = events.build_audit_event(
        service="crm", task_id=artifact_id, action="create_artifact",
        input_payload={
            "kind": req.kind,
            "owner_entity_kind": req.owner_entity_kind,
            "owner_entity_id": req.owner_entity_id,
            "title": (req.title or "")[:120],
            "source": req.source,
            "created_by": req.created_by,
            "has_storage_url": bool(req.storage_url),
            "has_inline_data": bool(req.inline_data),
            "bytes": req.bytes,
        },
        output_payload={
            "artifact_id": artifact_id,
            "kind": req.kind,
        },
        status="completed",
    )
    _audit(ev)

    return CreateArtifactResult(
        status="created", artifact_id=artifact_id, ts=ts,
    )


def find_opportunity_for_email(email: str) -> str | None:
    """Best-effort lookup: email -> contact -> prospect -> most-recent open opp.

    Used by the finance Stripe webhook to attribute an incoming payment back
    to a CRM opportunity. Returns None if any link in the chain is missing
    (no contact for the email, no prospect on the contact, no open
    opportunity for the prospect). Terminal-stage opportunities
    (``closed_won`` / ``closed_lost``) are filtered out — we only want
    open deals.
    """
    if not email or not email.strip():
        return None

    contact = _existing_contact_for_email(email)
    if contact is None or not contact.prospect_id:
        return None

    # Scan opportunities for this prospect.
    fe, vals, names = _filter_eq("prospect_id", contact.prospect_id)
    items, _trunc, _err = p.safe_scan(
        p._opportunities_table(), limit=50,
        filter_expression=fe,
        expression_attribute_values=vals,
        expression_attribute_names=names,
    )
    open_opps: list[Opportunity] = []
    for it in items:
        try:
            op = Opportunity.model_validate(it)
        except Exception:  # noqa: BLE001 — skip malformed rows
            continue
        if op.stage in TERMINAL_STAGES:
            continue
        open_opps.append(op)

    if not open_opps:
        return None

    # Most-recent open opportunity (created_at desc, updated_at desc as tiebreak).
    open_opps.sort(
        key=lambda o: (o.updated_at or "", o.created_at or ""),
        reverse=True,
    )
    return open_opps[0].opportunity_id


def close_opportunity_from_payment(
    opportunity_id: str,
    won_amount_usd: float,
    payment_ref: str,
    customer_email: str = "",
) -> AdvanceOpportunityResult:
    """Advance an opportunity to ``closed_won`` from a finance-side payment.

    When ``customer_email`` is supplied the function verifies the opportunity
    belongs to that customer before closing it — preventing an IDOR where a
    forged ``client_reference_id`` in a Stripe session could close an
    unrelated deal (CWE-639).
    """
    if customer_email:
        opp = get_opportunity(opportunity_id)
        if opp is None:
            _LOG.warning(
                "crm.close_opportunity_from_payment: opportunity %s not found "
                "(payment_ref=%s customer=%s)",
                opportunity_id, payment_ref, customer_email[-20:],
            )
            return AdvanceOpportunityResult(
                status="not_found",
                opportunity_id=opportunity_id,
                ts=iso_now(),
                error=f"opportunity_not_found: {opportunity_id}",
            )
        expected_prospect_id = None
        contact = _existing_contact_for_email(customer_email)
        if contact:
            expected_prospect_id = contact.prospect_id or None
        if expected_prospect_id is None:
            # PATCH-FIN-D7-01 — a customer_email was supplied for ownership
            # verification but resolves to no contact. We CANNOT confirm the
            # opportunity belongs to the payer, so closing it would advance an
            # arbitrary deal on an unverifiable claim. Fail closed (symmetric
            # with the ownership_mismatch branch below).
            _LOG.error(
                "crm.close_opportunity_from_payment: ownership UNVERIFIABLE — "
                "opportunity %s, payment customer %s resolves to no contact "
                "(payment_ref=%s)",
                opportunity_id, customer_email[-20:], payment_ref,
            )
            return AdvanceOpportunityResult(
                status="failed",
                opportunity_id=opportunity_id,
                ts=iso_now(),
                error="ownership_unverifiable",
            )
        if opp.prospect_id != expected_prospect_id:
            _LOG.error(
                "crm.close_opportunity_from_payment: IDOR attempt blocked — "
                "opportunity %s belongs to prospect %s but payment customer "
                "resolves to prospect %s (payment_ref=%s customer=%s)",
                opportunity_id, opp.prospect_id, expected_prospect_id,
                payment_ref, customer_email[-20:],
            )
            return AdvanceOpportunityResult(
                status="failed",
                opportunity_id=opportunity_id,
                ts=iso_now(),
                error="ownership_mismatch",
            )
    return advance_opportunity(AdvanceOpportunityRequest(
        opportunity_id=opportunity_id,
        target_stage="closed_won",
        won_amount_usd=float(won_amount_usd or 0.0),
        notes=f"payment_ref={payment_ref}",
    ))


# ---------------------------------------------------------------------------
# Feedback engine surface (Phase 6 — in-memory METRICS v1)
# ---------------------------------------------------------------------------


def log_feedback(req: FeedbackLogRequest) -> dict[str, str]:
    """Record one call outcome into the feedback engine.  Returns ``{"status": "logged"}``."""
    _LOG.info(
        "crm.log_feedback prospect=%s outcome=%s product=%s angle=%s objection=%s",
        req.prospect_id,
        req.outcome,
        req.product,
        req.angle,
        req.objection,
    )
    feedback_engine.log_interaction(
        prospect_id=req.prospect_id,
        outcome=req.outcome,
        objection=req.objection,
        product=req.product,
        angle=req.angle,
    )
    return {"status": "logged"}


def get_feedback_snapshot() -> FeedbackSnapshotResponse:
    """Return a plain-dict snapshot of all in-memory feedback counters."""
    snap = feedback_engine.snapshot()
    return FeedbackSnapshotResponse.model_validate(snap)


# ---------------------------------------------------------------------------
# Telemetry reads (additive, 2026-05-20)
# ---------------------------------------------------------------------------

def get_funnel_snapshot() -> dict[str, Any]:
    """Return the conversion-funnel leak-analysis snapshot.

    Thin pass-through to :func:`backend.common.conversion_funnel.funnel_snapshot`.
    The CRM lifecycle (convert_lead, create_opportunity, advance_opportunity)
    feeds the funnel ledger; this surfaces the aggregated per-stage counts +
    adjacent-stage conversion rates so the operator can see where deals leak.
    Degrades to an all-zero snapshot when the ledger is unreadable.
    """
    return conversion_funnel.funnel_snapshot()


# Upper bound on opportunity rows the telemetry aggregations scan. The
# per-stage / per-industry reads are full-table scans (Phase-1 volumes); this
# caps the read so a large table can't make a metrics endpoint unbounded.
_METRICS_SCAN_LIMIT = 500


def _scan_all_opportunities() -> tuple[list[Opportunity], bool, str | None]:
    """Scan up to ``_METRICS_SCAN_LIMIT`` opportunity rows for the metrics reads.

    Returns ``(rows, truncated, ddb_error)`` mirroring the ``*List`` shape.
    Malformed rows are skipped (logged) so one bad row can't sink a metric.
    """
    items, truncated, err = p.safe_scan(
        p._opportunities_table(), limit=_METRICS_SCAN_LIMIT,
    )
    rows: list[Opportunity] = []
    for it in items:
        try:
            rows.append(Opportunity.model_validate(it))
        except Exception as exc:  # noqa: BLE001 - skip malformed row
            _LOG.warning("metrics: skip malformed opportunity: %s", exc)
    return rows, truncated, err


def estimated_close_probability() -> dict[str, Any]:
    """Empirical close-probability telemetry over historical opportunities.

    Aggregates ``samus_opportunities`` into per-industry and per-stage
    EMPIRICAL conversion rates — how often a deal at vertical Y / stage X
    actually reached ``closed_won`` (``closed_won_retainer`` counts as won).
    Deterministic: same table state always yields the same numbers.

    A deal counts toward a stage bucket if its CURRENT stage equals that
    stage OR it is closed (a closed deal has, by definition, passed through
    its prior stages — but we only have the current stage on the row, so a
    stage bucket is "deals currently OR finally at this stage"). The rate is
    ``closed_won_count / total_in_bucket``.

    Returns a serialisable dict::

        {"by_industry": {industry: {"total": int, "won": int,
                                    "lost": int, "open": int,
                                    "close_probability": 0.0..1.0}},
         "by_stage": {stage: {"total": int, "won": int,
                              "close_probability": 0.0..1.0}},
         "sample_size": int,
         "scan_truncated": bool,
         "ddb_error": str | None}

    Degrades cleanly: a DDB error yields empty maps + the error string.
    """
    rows, truncated, err = _scan_all_opportunities()
    won_stages = {"closed_won", "closed_won_retainer"}

    by_industry: dict[str, dict[str, Any]] = {}
    by_stage: dict[str, dict[str, Any]] = {}

    for opp in rows:
        industry = (opp.industry or "").strip().lower() or "(unspecified)"
        ind = by_industry.setdefault(
            industry, {"total": 0, "won": 0, "lost": 0, "open": 0},
        )
        ind["total"] += 1
        if opp.stage in won_stages:
            ind["won"] += 1
        elif opp.stage == "closed_lost":
            ind["lost"] += 1
        else:
            ind["open"] += 1

        stg = by_stage.setdefault(opp.stage, {"total": 0, "won": 0})
        stg["total"] += 1
        if opp.stage in won_stages:
            stg["won"] += 1

    for ind in by_industry.values():
        total = ind["total"]
        ind["close_probability"] = (
            round(ind["won"] / total, 4) if total > 0 else 0.0
        )
    for stg in by_stage.values():
        total = stg["total"]
        stg["close_probability"] = (
            round(stg["won"] / total, 4) if total > 0 else 0.0
        )

    return {
        "by_industry": by_industry,
        "by_stage": by_stage,
        "sample_size": len(rows),
        "scan_truncated": truncated,
        "ddb_error": err,
    }


def token_cost_by_industry() -> dict[str, Any]:
    """Per-vertical discovery-LLM cost roll-up over historical opportunities.

    Every Opportunity carries ``token_cost_usd`` (the discovery LLM spend
    captured at creation) + ``industry``. This aggregates total + mean cost
    per industry so the operator can see which verticals are expensive to
    discover. Deterministic; degrades to empty maps on a DDB error.

    Returns a serialisable dict::

        {"by_industry": {industry: {"opportunity_count": int,
                                    "total_token_cost_usd": float,
                                    "mean_token_cost_usd": float}},
         "grand_total_token_cost_usd": float,
         "sample_size": int,
         "scan_truncated": bool,
         "ddb_error": str | None}
    """
    rows, truncated, err = _scan_all_opportunities()

    by_industry: dict[str, dict[str, Any]] = {}
    grand_total = 0.0
    for opp in rows:
        industry = (opp.industry or "").strip().lower() or "(unspecified)"
        cost = float(opp.token_cost_usd or 0.0)
        bucket = by_industry.setdefault(
            industry, {"opportunity_count": 0, "total_token_cost_usd": 0.0},
        )
        bucket["opportunity_count"] += 1
        bucket["total_token_cost_usd"] += cost
        grand_total += cost

    for bucket in by_industry.values():
        count = bucket["opportunity_count"]
        bucket["total_token_cost_usd"] = round(bucket["total_token_cost_usd"], 6)
        bucket["mean_token_cost_usd"] = (
            round(bucket["total_token_cost_usd"] / count, 6) if count > 0 else 0.0
        )

    return {
        "by_industry": by_industry,
        "grand_total_token_cost_usd": round(grand_total, 6),
        "sample_size": len(rows),
        "scan_truncated": truncated,
        "ddb_error": err,
    }


def daily_stats(
    today: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """CallState rows touched on ``today``, bucketed by outcome.

    Two selection modes:
      * DEFAULT (``start``/``end`` omitted): ``begins_with(updated_at, today)``
        — matches the UTC calendar day. Kept for backward compatibility.
      * RANGE (``start`` and ``end`` both given as UTC ISO-8601): scans
        ``updated_at BETWEEN :start AND :end``. This is how a PACIFIC business
        day is selected — a Pacific day maps to a UTC *range*, not a UTC prefix
        (see ``backend.common.dates.business_day_utc_bounds``). ``today`` is
        then just the label echoed back in ``date``.

    Counts:
      * ``calls_today``     — every row touched (operator-hand-dialed +
        voice-agent + scheduled-send; the upsert path is the same for all three).
      * ``booked_today``    — rows whose ``last_outcome == "booked"``.
      * ``followups_today`` — rows whose ``last_outcome == "follow_up"``.

    Powers the gateway's ``GET /api/crm/stats`` (the forge-ui Samus HUD's daily
    counter). Degrades cleanly: a DDB outage returns zeroes + ``ddb_error`` so
    the dashboard surfaces "no data" rather than 500-ing. Best-effort scan
    bound at 500 rows / 20 pages — way above today's call volume; a future
    busier day would just surface ``scan_truncated=True``.
    """
    if start and end:
        fe = "#u BETWEEN :start AND :end"
        eav = {":start": start, ":end": end}
    else:
        fe = "begins_with(#u, :today)"
        eav = {":today": today}
    items, truncated, err = p.safe_scan(
        p._call_state_table(), limit=500,
        filter_expression=fe,
        expression_attribute_values=eav,
        expression_attribute_names={"#u": "updated_at"},
    )
    calls = booked = followups = 0
    for it in items:
        try:
            row = CallState.model_validate(it)
        except Exception as exc:  # noqa: BLE001 - skip malformed
            _LOG.warning("daily_stats skip malformed call_state: %s", exc)
            continue
        calls += 1
        oc = (row.last_outcome or "").strip().lower()
        if oc == "booked":
            booked += 1
        elif oc == "follow_up":
            followups += 1
    return {
        "date": today,
        "calls_today": calls,
        "booked_today": booked,
        "followups_today": followups,
        "scan_truncated": truncated,
        "ddb_error": err,
    }


# ---------------------------------------------------------------------------
# Phase 6 — Opportunity auto-create + OperatorTask lifecycle auto-generators
# ---------------------------------------------------------------------------


def _persist_lifecycle_tasks(tasks: list[CreateOperatorTaskRequest], context: str) -> int:
    """Persist each lifecycle-generated task; log+swallow failures so a task hiccup
    never fails the opportunity it was generated for. Returns success count."""
    created = 0
    for task_req in tasks:
        try:
            create_operator_task(task_req)
            created += 1
        except Exception as exc:
            _LOG.warning(
                "lifecycle task creation failed (best-effort) context=%s error=%s",
                context, exc,
            )
    return created


def auto_create_opportunity_from_lead(
    prospect_id: str,
    contact_id: str,
    intent_score: int | None,
    monthly_budget: str,
    service_interest: list[str],
    assigned_to: str = "",
) -> CreateOpportunityResult:
    """Auto-create an Opportunity from lead signals + fire lifecycle task auto-generators.

    Builds a :class:`CreateOpportunityRequest` from inputs and routes through the
    existing :func:`create_opportunity` (which itself runs the
    :func:`scoring.score_opportunity_from_lead` tier/probability/deal-size math).
    After successful creation, lifecycle.tasks_for_new_opportunity is called and
    each task persisted best-effort.
    """
    req = CreateOpportunityRequest(
        prospect_id=prospect_id,
        contact_id=contact_id,
        intent_score=intent_score,
        monthly_budget=monthly_budget,
        service_interest=list(service_interest),
        assigned_to=assigned_to or _DEFAULT_AUTO_ASSIGNEE,
    )
    result = create_opportunity(req)

    if result.status == "created":
        opp = get_opportunity(result.opportunity_id)
        if opp is not None:
            tasks = lifecycle.tasks_for_new_opportunity(opp, intent_score)
            tasks_count = _persist_lifecycle_tasks(tasks, "auto_create_opportunity_from_lead")
            ev = events.build_audit_event(
                service="crm", task_id=result.opportunity_id, action="opportunity.auto_created",
                input_payload={
                    "prospect_id": prospect_id,
                    "intent_score": intent_score,
                    "source": "auto_create_from_lead",
                },
                output_payload={
                    "opportunity_id": result.opportunity_id,
                    "tasks_count": tasks_count,
                },
                status="completed",
            )
            _audit(ev)

    return result


def auto_create_opportunity_from_deal_scoring(
    prospect_id: str,
    contact_id: str,
    intel: dict,
    engagement: dict | None = None,
) -> CreateOpportunityResult:
    """Auto-create an Opportunity driven by the prospecting deal_scoring tier.

    Calls :func:`backend.prospecting.deal_scoring.score_deal` and skips creation
    for ``cold`` tier (returns a synthetic failed result so callers can branch on
    it). Warm/nurture/hot tiers create an Opportunity with intent_score set to
    the deal_scoring priority_score, then run the same lifecycle pipeline as
    :func:`auto_create_opportunity_from_lead`.
    """
    from backend.prospecting.deal_scoring import score_deal

    score = score_deal(intel, engagement)
    tier: str = score["tier"]
    priority_score: int = score["priority_score"]

    if tier == "cold":
        return CreateOpportunityResult(
            status="failed",
            opportunity_id="",
            ts=iso_now(),
            error="cold_tier_no_opportunity_created",
        )

    req = CreateOpportunityRequest(
        prospect_id=prospect_id,
        contact_id=contact_id,
        intent_score=priority_score,
        monthly_budget="",
        service_interest=[],
        assigned_to=_DEFAULT_AUTO_ASSIGNEE,
    )
    result = create_opportunity(req)

    if result.status == "created":
        opp = get_opportunity(result.opportunity_id)
        if opp is not None:
            tasks = lifecycle.tasks_for_new_opportunity(opp, priority_score)
            tasks_count = _persist_lifecycle_tasks(
                tasks, "auto_create_opportunity_from_deal_scoring"
            )
            ev = events.build_audit_event(
                service="crm", task_id=result.opportunity_id, action="opportunity.auto_created",
                input_payload={
                    "prospect_id": prospect_id,
                    "tier": tier,
                    "priority_score": priority_score,
                    "probability": score["probability"],
                    "source": "auto_create_from_deal_scoring",
                },
                output_payload={
                    "opportunity_id": result.opportunity_id,
                    "tasks_count": tasks_count,
                },
                status="completed",
            )
            _audit(ev)

    return result


def advance_opportunity_with_lifecycle(
    req: AdvanceOpportunityRequest,
) -> AdvanceOpportunityResult:
    """Advance an Opportunity stage AND fire lifecycle task auto-generators.

    Thin wrapper over :func:`advance_opportunity` for Phase 6 callers. The
    underlying advance_opportunity is left unchanged for back-compat with the
    existing per-stage callers (finance close_opportunity_from_payment etc.).
    After a successful advance, runs lifecycle.tasks_for_stage_advance and
    persists each generated task best-effort.
    """
    opp_before = get_opportunity(req.opportunity_id)
    prior_stage = opp_before.stage if opp_before is not None else ""

    result = advance_opportunity(req)

    if result.status == "advanced":
        opp_after = get_opportunity(req.opportunity_id)
        if opp_after is not None:
            tasks = lifecycle.tasks_for_stage_advance(
                opp_after, prior_stage, result.new_stage,
            )
            _persist_lifecycle_tasks(
                tasks, f"advance_opportunity_with_lifecycle:{result.new_stage}",
            )

    return result
