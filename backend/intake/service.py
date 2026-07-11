"""Intake service — validate, dedup, persist, audit onboarding leads."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import Any

from backend.common import aws, events, persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.email_backend import EmailBackendError, send_email
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from .models import (
    LeadListResult,
    OnboardingLeadRequest,
    OnboardingLeadResult,
    StoredLead,
)


_LOG = logging.getLogger("samus.intake.service")
_AUDIT_PATH_DEFAULT = "/opt/samus/data/intake/intake_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(
        os.getenv("SAMUS_INTAKE_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def _leads_table() -> Any:
    settings = get_settings()
    return aws.table(settings.ddb_onboarding_leads_table, settings.aws_region)


def _new_lead_id() -> str:
    return f"lead_{uuid.uuid4().hex[:20]}"


def _dedup_key(email: str, website_url: str, pain_points: str) -> str:
    """Stable fingerprint for duplicate-submission detection.

    Truncates pain_points to first 200 chars so trailing edits (typo fixes,
    extra punctuation) still count as the same lead. Lowercases email so
    "Alex@x.com" and "alex@x.com" collapse.
    """
    canon = "|".join([
        (email or "").strip().lower(),
        (website_url or "").strip().lower(),
        (pain_points or "")[:200],
    ])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _safe_put(table: Any, item: dict[str, Any]) -> tuple[bool, str | None]:
    """Wrap put_item so AWS misconfig doesn't lose the lead.

    Returns (persisted, error_string). On failure the audit ledger still
    captures the lead so it can be replayed later.
    """
    try:
        table.put_item(Item=item)
        return True, None
    except Exception as exc:  # noqa: BLE001 — degraded path
        _LOG.warning("intake put_item failed: %s", exc)
        return False, f"ddb_put_failed: {exc}"


# Finding 2 — operator-email sanitization.
# The lead fields (name, company, pain_points, website_url, email) are
# attacker-controlled. Two abuse vectors must be neutralized:
#   1. Header / subject injection — CR/LF (and other control chars) in a
#      value that lands on the Subject line could split the header.
#   2. Structured-line forgery — the body uses operator-authored marker
#      lines ("STATUS: ...", "lead_id: ...", "--- Metadata ---"). An attacker
#      who embeds newlines + a fake "STATUS: PERSISTED" inside pain_points
#      could mislead an operator skim-reading the mirror.
# Defence: strip control chars from every value; for the multi-line
# pain_points field, indent every line and wrap it in explicit BEGIN/END
# fences so a forged marker can never be mistaken for an operator-authored
# line. ``submit_lead`` output today reaches only the plain-text operator
# email (this boundary) — there is currently no HTML / markdown surface; if
# one is added, HTML-escape these same fields at that new boundary.

# Allow tab + the printable range; drop every other C0/C1 control char.
_ALLOWED_CONTROL = {"\t"}


def _strip_control_chars(value: str) -> str:
    """Remove control characters (including CR/LF) from a single-line value.

    Used for any lead field that enters the email Subject line or a
    single-line body field. Tabs are preserved (harmless, sometimes part of
    legitimate input); every other C0/C1 control code is dropped.
    """
    return "".join(
        ch for ch in (value or "")
        if ch in _ALLOWED_CONTROL or (ord(ch) >= 0x20 and ord(ch) != 0x7F)
    )


def _fence_untrusted_block(label: str, value: str) -> str:
    """Render a multi-line untrusted value inside explicit BEGIN/END fences.

    Every line of the value is prefixed with ``| `` so it is visually and
    structurally distinct from operator-authored lines — a forged
    ``STATUS: PERSISTED`` smuggled inside the value renders as
    ``| STATUS: PERSISTED`` and cannot be confused with the real status
    line. Control chars other than the line breaks themselves are stripped.
    """
    # Normalize line endings, then strip control chars per physical line so
    # the structure (the newlines) survives but injected control codes do not.
    raw = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_strip_control_chars(line) for line in raw.split("\n")]
    indented = "\n".join(f"| {line}" for line in lines)
    return (
        f"--- BEGIN {label} (lead-supplied, untrusted) ---\n"
        f"{indented}\n"
        f"--- END {label} ---"
    )


def _format_operator_email_body(
    stored: StoredLead,
    *,
    persisted: bool,
    ddb_error: str | None,
) -> tuple[str, str]:
    """Build (subject, plain_body) for the operator mirror email.

    The body is structured so an operator can read the lead at a glance and
    re-key it into DDB by hand if the workcell couldn't persist (degraded
    path). Persistence status is surfaced at the top so a failed write is
    immediately visible without scrolling.

    All lead-supplied fields are sanitized (see the module-level note on
    Finding 2): control chars are stripped from single-line fields before
    they reach the Subject or a body line, and the multi-line ``pain_points``
    field is fenced so an attacker cannot forge operator-authored marker
    lines.
    """
    persist_line = (
        "STATUS: PERSISTED to samus_onboarding_leads"
        if persisted
        else f"STATUS: DDB WRITE FAILED -- {_strip_control_chars(ddb_error or 'unknown')}\n"
             "ACTION: re-key from this email when DDB is back up."
    )
    # Subject-line fields — CR/LF stripped to prevent header injection.
    safe_email = _strip_control_chars(stored.email)
    safe_company = _strip_control_chars(stored.company) or "(no company)"
    subject = f"[Hustleforge] New onboarding lead: {safe_company} <{safe_email}>"
    # Single-line body fields — control chars stripped so a value cannot
    # introduce its own newline + a forged marker line.
    safe_name = _strip_control_chars(stored.name)
    safe_website = _strip_control_chars(stored.website_url) or "(blank)"
    services = ", ".join(
        _strip_control_chars(s) for s in stored.service_interest
    ) or "(none selected)"
    safe_budget = _strip_control_chars(stored.monthly_budget) or "(blank)"
    safe_timeline = _strip_control_chars(stored.timeline) or "(blank)"
    safe_user_agent = _strip_control_chars(stored.user_agent) or "(unknown)"
    safe_source_ip = _strip_control_chars(stored.source_ip) or "(unknown)"
    # Multi-line untrusted field — fenced + per-line prefixed.
    pain_block = _fence_untrusted_block("Pain points", stored.pain_points)
    body = (
        f"{persist_line}\n"
        f"\n"
        f"lead_id:        {stored.lead_id}\n"
        f"created_at:     {stored.created_at}\n"
        f"\n"
        f"Name:           {safe_name}\n"
        f"Email:          {safe_email}\n"
        f"Company:        {safe_company}\n"
        f"Website:        {safe_website}\n"
        f"\n"
        f"Service interest: {services}\n"
        f"Monthly budget:   {safe_budget}\n"
        f"Timeline:         {safe_timeline}\n"
        f"\n"
        f"{pain_block}\n"
        f"\n"
        f"--- Metadata ---\n"
        f"source_ip:  {safe_source_ip}\n"
        f"user_agent: {safe_user_agent}\n"
        f"dedup_key:  {stored.dedup_key}\n"
    )
    return subject, body


def _send_operator_mirror(
    stored: StoredLead,
    *,
    persisted: bool,
    ddb_error: str | None,
) -> tuple[bool, str | None]:
    """Best-effort SES email to the operator inbox.

    Never raises. Returns (sent_ok, error_string). When
    ``settings.intake_operator_email`` is empty the mirror is a no-op and
    returns ``(False, None)`` — empty config is not an error.
    """
    settings = get_settings()
    to_addr = (settings.intake_operator_email or "").strip()
    if not to_addr:
        return False, None
    subject, body = _format_operator_email_body(
        stored, persisted=persisted, ddb_error=ddb_error,
    )
    try:
        send_email(to=to_addr, subject=subject, body=body, message_kind="transactional")
        return True, None
    except EmailBackendError as exc:
        _LOG.warning("intake operator email mirror failed: %s", exc)
        return False, f"email_send_failed: {exc}"
    except Exception as exc:  # noqa: BLE001 — never block on mirror
        _LOG.warning("intake operator email mirror raised: %s", exc)
        return False, f"email_unexpected: {exc}"


def submit_lead(
    req: OnboardingLeadRequest,
    *,
    source_ip: str = "",
    user_agent: str = "",
) -> OnboardingLeadResult:
    """Persist one onboarding lead. Dedup-aware, never raises on persistence.

    Decision flow:
      1. compute dedup_key from email + website_url + pain[:200]
      2. if seen inside settings.intake_dedup_window_seconds -> return duplicate
      3. else: mint lead_id, build StoredLead, try DDB put_item
      4. always append to JSONL audit ledger (the lead is never lost)
      5. mark dedup_key first-seen on the idempotency store
    """
    settings = get_settings()
    ts = iso_now()

    dedup_key = _dedup_key(req.email, req.website_url, req.pain_points)
    cache_key = f"intake.dedup:{dedup_key}"

    if not GLOBAL_IDEMPOTENCY_STORE.first_seen(cache_key):
        # Same lead inside the window. Audit the duplicate so the count is
        # visible to ops; skip the DDB write to avoid hot-spotting a single
        # row + to keep accidental double-clicks out of the lead list.
        ev = events.build_audit_event(
            service="intake",
            task_id=dedup_key[:16],
            action="submit_lead",
            input_payload={
                "email_tail": (req.email or "")[-12:],
                "dedup_key": dedup_key,
                "service_interest_count": len(req.service_interest),
            },
            output_payload={"status": "duplicate"},
            status="duplicate",
        )
        try:
            _audit_ledger().append(ev)
        except OSError as exc:
            _LOG.warning("intake audit append failed: %s", exc)
        return OnboardingLeadResult(
            status="duplicate",
            lead_id="",
            ts=ts,
            persisted=False,
            audit_appended=True,
        )

    lead_id = _new_lead_id()
    stored = StoredLead(
        lead_id=lead_id,
        created_at=ts,
        name=req.name,
        email=req.email,
        company=req.company,
        website_url=req.website_url,
        service_interest=list(req.service_interest),
        pain_points=req.pain_points,
        monthly_budget=req.monthly_budget,
        timeline=req.timeline,
        source_ip=source_ip,
        user_agent=user_agent[:512],
        dedup_key=dedup_key,
    )

    persisted = False
    ddb_error: str | None = None
    try:
        tbl = _leads_table()
    except Exception as exc:  # noqa: BLE001 — boto bootstrap failure
        _LOG.warning("intake leads table lookup failed: %s", exc)
        ddb_error = f"ddb_bootstrap_failed: {exc}"
        tbl = None
    if tbl is not None:
        persisted, ddb_error = _safe_put(tbl, stored.model_dump())

    audit_ok = False
    ev = events.build_audit_event(
        service="intake",
        task_id=lead_id,
        action="submit_lead",
        input_payload={
            "email_tail": (req.email or "")[-12:],
            "company": (req.company or "")[:64],
            "service_interest": list(req.service_interest),
            "monthly_budget": req.monthly_budget,
            "timeline": req.timeline,
            "source_ip": source_ip,
            "dedup_key": dedup_key,
        },
        output_payload={
            "lead_id": lead_id,
            "persisted": persisted,
            "ddb_error": ddb_error,
        },
        status="completed" if persisted else "degraded",
    )
    try:
        _audit_ledger().append(ev)
        audit_ok = True
    except OSError as exc:
        _LOG.warning("intake audit append failed: %s", exc)

    # Unified business-event ledger (HOTL Tranche 1) — one lead.created row
    # per accepted (non-duplicate) lead. The prospect id doesn't exist yet at
    # intake time, so the lead_id rides in metadata for later joining via the
    # CRM convert event. Fail-soft by contract: emit_business_event never
    # raises.
    from backend.common.business_events import LEAD_CREATED, emit_business_event
    emit_business_event(
        LEAD_CREATED,
        workcell="intake",
        metadata={
            "lead_id": lead_id,
            "email_tail": (req.email or "")[-12:],
            "company": (req.company or "")[:64],
            "source": "onboarding_form",
            "persisted": persisted,
        },
    )

    # Operator email mirror — fires on every accepted lead (queued OR
    # degraded). Best-effort: a SendGrid failure does NOT change the response
    # the caller sees. The email is the "even if DDB fell over" failsafe so
    # the operator always has a copy of every order, no matter what.
    _send_operator_mirror(stored, persisted=persisted, ddb_error=ddb_error)

    # Warm-lead enrollment for website-sourced leads. Wire-not-arm — gated on
    # OUTREACH_WEBSITE_FORM_WARM_ENROLL_ENABLED (default OFF). Without this
    # path a form submission lands in DDB and never enters any warm cadence,
    # which is the CODB-funding leak the operator flagged: the site Samus owns
    # was silently discarding self-declared buying intent. Best-effort — a
    # buying_signal_route fault must never fail the intake response.
    try:
        from backend.outreach.buying_signal_route import (
            maybe_enroll_buying_signal_from_website_form,
        )
        maybe_enroll_buying_signal_from_website_form(
            stored_lead=stored, now_iso=ts,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail intake
        _LOG.warning("website_form warm-lead enrollment failed: %s", exc)

    return OnboardingLeadResult(
        status="queued" if persisted else "degraded",
        lead_id=lead_id,
        ts=ts,
        persisted=persisted,
        audit_appended=audit_ok,
        error=ddb_error,
    )


def list_recent_leads(limit: int = 50) -> LeadListResult:
    """Operator view — recent leads, scan-based (small table, infrequent call)."""
    clamped = max(1, min(200, int(limit)))
    try:
        tbl = _leads_table()
    except Exception as exc:  # noqa: BLE001 — boto bootstrap failure
        _LOG.warning("intake leads table lookup failed: %s", exc)
        return LeadListResult(ddb_error=f"ddb_bootstrap_failed: {exc}")
    try:
        resp = tbl.scan(Limit=clamped)
    except Exception as exc:  # noqa: BLE001 — degraded path
        _LOG.warning("intake leads scan failed: %s", exc)
        return LeadListResult(ddb_error=f"ddb_scan_failed: {exc}")
    items = resp.get("Items", []) or []
    leads: list[StoredLead] = []
    for item in items:
        try:
            leads.append(StoredLead.model_validate(item))
        except Exception as exc:  # noqa: BLE001 — skip malformed row
            _LOG.warning("skipping malformed lead row: %s", exc)
    leads.sort(key=lambda x: x.created_at, reverse=True)
    return LeadListResult(
        leads=leads,
        count=len(leads),
        scan_truncated="LastEvaluatedKey" in resp,
    )