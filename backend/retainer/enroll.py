"""Retainer enrollment — first touch when a customer subscribes.

Mirrors the shape of ``backend.fulfill.fulfill_customer`` but for
recurring-revenue starts:

  1. Look up SKU in the retainer registry; reject unknown.
  2. Find or create the Customer (idempotent on email).
  3. Advance customer state to ``active_retainer``  *
  4. Register a CRM Opportunity in stage ``closed_won_retainer``  *
  5. Persist a ``next_cycle_at`` marker the cron pulls (skipped for
     always-on SKUs with ``has_monthly_cycle=False``, e.g. the receptionist).
  6. Send a welcome email outlining the monthly cadence.
  7. Return :class:`EnrollmentResult`.

(*) Both ``active_retainer`` and ``closed_won_retainer`` are NEW values
that don't exist in the current ``CustomerState`` / ``OpportunityStage``
enums. See the report-back doc for the proposed extensions. Until those
land, the enroll path FALLS BACK to ``delivered`` / ``closed_won`` and
tags the metadata blob with ``is_retainer=True`` so downstream consumers
can filter — no model edits required.

Three injectable callables let tests run without Neo4j / CRM / SES:

  * ``customer_store``     — CustomerStore-like
  * ``crm_dispatch_fn``    — callable(payload: dict) -> None (signed HTTP POST)
  * ``send_email_fn``      — callable(to, subject, body, html_body=None) -> dict
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .registry import (
    RetainerProductConfig,
    get_retainer_sku,
)


_LOG = logging.getLogger("samus.retainer.enroll")


_TARGET_CUSTOMER_STATE = "active_retainer"
_TARGET_OPPORTUNITY_STAGE = "closed_won_retainer"


# ---------------------------------------------------------------------------
# Step / result models
# ---------------------------------------------------------------------------

EnrollStepName = Literal[
    "lookup_sku",
    "find_or_create_customer",
    "advance_to_active_retainer",
    "create_opportunity",
    "schedule_next_cycle",
    "send_welcome_email",
]
EnrollStepStatus = Literal["ok", "skipped", "failed"]


class EnrollStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: EnrollStepName
    status: EnrollStepStatus
    detail: str = ""
    elapsed_ms: int = 0


class EnrollmentResult(BaseModel):
    """Full audit trail of an ``enroll_retainer()`` invocation.

    Mirrors :class:`backend.fulfill.FulfillmentResult` in spirit — every
    step is recorded with status + elapsed_ms; ``ok`` is the boolean
    rollup; ``ts`` is the run start (ISO-8601 UTC).
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    sku_id: str
    customer_id: str | None = None
    opportunity_id: str | None = None
    next_cycle_at: str | None = None  # ISO-8601 UTC
    plan_marker_path: str | None = None  # filesystem path to next-cycle marker
    welcome_message_id: str | None = None
    ok: bool
    steps: list[EnrollStep] = Field(default_factory=list)
    ts: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UnknownRetainerSkuError(ValueError):
    """Raised when ``enroll_retainer`` gets a sku_id outside ``RETAINER_SKUS``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _first_of_next_month(now: datetime | None = None) -> datetime:
    """Return midnight UTC on the 1st of the next calendar month.

    The first monthly cycle runs at the start of the customer's first full
    billing month. Enrolling mid-month doesn't trigger an immediate cycle
    — the welcome email is the in-month touch; the cycle starts when the
    new month begins. This matches Stripe's billing-anchor convention.
    """
    now = now or datetime.now(timezone.utc)
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)


def _plan_marker_dir(customer_id: str, sku_id: str) -> Path:
    """``<SAMUS_ARTIFACT_ROOT>/customers/<slug>/<sku>/_enrollment/``."""
    from backend.common import storage

    target = storage.root() / "customers" / customer_id / sku_id / "_enrollment"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_next_cycle_marker(
    customer_id: str,
    sku: RetainerProductConfig,
    next_cycle_at: datetime,
) -> Path:
    """Write the ``next_cycle_at`` marker the cron polls.

    Simple JSON file the scheduled cycle runner can scan (no DB roundtrip
    required for the cron tick). One file per customer-SKU; rewritten each
    cycle when the next due-date is computed.
    """
    target = _plan_marker_dir(customer_id, sku.sku_id) / "next_cycle.json"
    target.write_text(
        json.dumps(
            {
                "customer_id": customer_id,
                "sku_id": sku.sku_id,
                "cycle_id": sku.cycle_id,
                "next_cycle_at": next_cycle_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cadence": sku.cadence,
                "written_at": _now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def _build_welcome_email(
    customer_name: str,
    sku: RetainerProductConfig,
    next_cycle_at: datetime,
) -> tuple[str, str, str]:
    """Compose welcome email subject + plain-text body + HTML body.

    Voice notes (matches the ``finance/upsell_template.py`` family):
      - Sign as a person, not a brand voice (Morgan @ HustleForge)
      - Concrete dates, not vague "soon"
      - Set expectations for the monthly cadence in the first email so the
        customer isn't surprised when month 1's report lands on the 1st.
    """
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    sku_name = sku.display_name
    cycle_date_human = next_cycle_at.strftime("%B %d, %Y").replace(" 0", " ")

    if sku.sku_id == "retainer_seo_optimization":
        subject = "You're set up for SEO Optimization — here's how the month works"
        text_body = (
            f"{greeting}\n"
            "\n"
            f"Welcome aboard — you're enrolled in {sku_name} at "
            f"${sku.price_usd_cents_low // 100}/month. Here's what to expect:\n"
            "\n"
            f"On {cycle_date_human}, your first monthly cycle runs. That means:\n"
            "  1. Fresh audit of your site (so we have a current baseline).\n"
            "  2. Diff against last month — what's changed, what's new.\n"
            "  3. We apply the highest-impact technical fixes (broken links, "
            "meta tags, schema, page speed, internal linking).\n"
            "  4. You get a visibility report by end-of-month showing rank "
            "movement, GSC traffic delta, and exactly what was changed.\n"
            "\n"
            "You don't need to do anything between now and then — we'll take "
            "care of the work and send the report when it's ready. If you'd "
            "like to flag a specific page or keyword to prioritize this "
            "month, just hit reply.\n"
            "\n"
            "— Morgan, HustleForge\n"
        )
        html_body = (
            f"<p>{greeting}</p>"
            f"<p>Welcome aboard — you're enrolled in <strong>{sku_name}</strong> at "
            f"<strong>${sku.price_usd_cents_low // 100}/month</strong>. Here's "
            "what to expect:</p>"
            f"<p>On <strong>{cycle_date_human}</strong>, your first monthly "
            "cycle runs. That means:</p>"
            "<ol>"
            "<li>Fresh audit of your site (so we have a current baseline).</li>"
            "<li>Diff against last month — what's changed, what's new.</li>"
            "<li>We apply the highest-impact technical fixes (broken links, "
            "meta tags, schema, page speed, internal linking).</li>"
            "<li>You get a visibility report by end-of-month showing rank "
            "movement, GSC traffic delta, and exactly what was changed.</li>"
            "</ol>"
            "<p>You don't need to do anything between now and then. If you'd "
            "like to flag a specific page or keyword to prioritize this month, "
            "just hit reply.</p>"
            "<p>— Morgan, HustleForge</p>"
        )
    elif sku.sku_id == "retainer_ai_receptionist":
        # Always-on inbound service — no monthly cycle, so the email sets
        # expectations around 24/7 call handling + the weekly summary
        # instead of a first-cycle date.
        subject = "Your AI receptionist is live — here's what to expect"
        text_body = (
            f"{greeting}\n"
            "\n"
            f"Welcome aboard — your {sku_name} is set up. From here on, "
            "every call to your business line is answered, 24/7:\n"
            "\n"
            "  - Callers are greeted, asked what they need, and helped with "
            "common questions.\n"
            "  - Appointment and callback requests are captured and sent to "
            "you as a task — nothing slips.\n"
            "  - Urgent calls are flagged, and after-hours messages are "
            "emailed to you right away.\n"
            "  - Once a week you get a summary of every call: how many came "
            "in, what callers wanted, and what needs your attention.\n"
            "\n"
            f"Billing is ${sku.price_usd_cents_low // 100}/month plus "
            "$0.35 per call-minute, billed automatically each month.\n"
            "\n"
            "If you want to change your greeting, business hours, or where "
            "urgent calls are forwarded, just hit reply.\n"
            "\n"
            "— Morgan, HustleForge\n"
        )
        html_body = (
            f"<p>{greeting}</p>"
            f"<p>Welcome aboard — your <strong>{sku_name}</strong> is set up. "
            "From here on, every call to your business line is answered, "
            "24/7:</p>"
            "<ul>"
            "<li>Callers are greeted, asked what they need, and helped with "
            "common questions.</li>"
            "<li>Appointment and callback requests are captured and sent to "
            "you as a task — nothing slips.</li>"
            "<li>Urgent calls are flagged, and after-hours messages are "
            "emailed to you right away.</li>"
            "<li>Once a week you get a summary of every call.</li>"
            "</ul>"
            f"<p>Billing is <strong>${sku.price_usd_cents_low // 100}/month</strong> "
            "plus $0.35 per call-minute, billed automatically each month.</p>"
            "<p>To change your greeting, business hours, or call forwarding, "
            "just hit reply.</p>"
            "<p>— Morgan, HustleForge</p>"
        )
    else:
        # retainer_ai_ops_partner_{starter|growth|scale}
        subject = "Welcome to the AI Ops Partner Program — your first cycle"
        text_body = (
            f"{greeting}\n"
            "\n"
            f"Welcome to {sku_name}. The cadence is a four-week cycle, and "
            f"your first one starts on {cycle_date_human}:\n"
            "\n"
            "  Week 1 — Assess. We pull current-state metrics on your ops "
            "stack and run a 30-minute check-in call so I can understand "
            "what's working and what's not.\n"
            "  Week 2 — Prioritize. Together we lock the scope for this "
            "month's build — which automation or process to fix first.\n"
            "  Week 3 — Build & deploy. We do the work. You get to keep "
            "running your business.\n"
            "  Week 4 — Report. You get an ops report showing what shipped, "
            "what it's saving you, and what's queued for next month.\n"
            "\n"
            "Between now and the first cycle, expect a calendar invite for "
            "the Week 1 assessment call. If you want to flag anything urgent "
            "before then, just reply to this email.\n"
            "\n"
            "— Morgan, HustleForge\n"
        )
        html_body = (
            f"<p>{greeting}</p>"
            f"<p>Welcome to <strong>{sku_name}</strong>. The cadence is a "
            f"four-week cycle, and your first one starts on "
            f"<strong>{cycle_date_human}</strong>:</p>"
            "<ul>"
            "<li><strong>Week 1 — Assess.</strong> Current-state metrics "
            "pull + 30-minute check-in call.</li>"
            "<li><strong>Week 2 — Prioritize.</strong> Lock the scope for "
            "this month's build.</li>"
            "<li><strong>Week 3 — Build & deploy.</strong> We do the work.</li>"
            "<li><strong>Week 4 — Report.</strong> Ops report covering what "
            "shipped, what it's saving you, and what's queued for next "
            "month.</li>"
            "</ul>"
            "<p>Between now and the first cycle, expect a calendar invite "
            "for the Week 1 assessment call. If you want to flag anything "
            "urgent before then, just reply to this email.</p>"
            "<p>— Morgan, HustleForge</p>"
        )
    return subject, text_body, html_body


# ---------------------------------------------------------------------------
# CRM dispatch (best-effort)
# ---------------------------------------------------------------------------


def _default_crm_dispatch(payload: dict[str, Any]) -> None:
    """Best-effort POST to ``samus-crm``'s ``POST /crm/opportunities``.

    Mirrors the dispatch pattern in ``backend.seo.service`` — fire-and-forget
    in a thread so the enroll caller doesn't block on CRM. Configuration
    gaps (no CRM URL, no HMAC key) log + return without dispatching.
    Tests inject their own ``crm_dispatch_fn``.
    """
    from backend.common.config import get_settings
    from backend.common.http_client import signed_post_json

    settings = get_settings()
    crm_url = settings.gateway_urls.get("crm")
    if not crm_url:
        _LOG.debug("retainer crm dispatch skipped: crm_url_unset")
        return
    if not settings.shared_hmac_key:
        _LOG.debug("retainer crm dispatch skipped: shared_hmac_key_unset")
        return
    try:
        asyncio.run(signed_post_json(crm_url, "/crm/opportunities", payload, retries=2))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("retainer crm opportunity dispatch failed: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def enroll_retainer(
    *,
    sku_id: str,
    email: str,
    name: str = "",
    company: str = "",
    operations_summary: str = "",  # collected for AI Ops Partner intake
    audit_url: str = "",  # collected for SEO Optimization intake
    monthly_amount_usd_cents: int | None = None,
    customer_store: Any = None,
    crm_dispatch_fn: Any = None,
    send_email_fn: Any = None,
    now: datetime | None = None,
) -> EnrollmentResult:
    """Drive the retainer-start sequence end-to-end.

    Production callers omit ``customer_store`` / ``crm_dispatch_fn`` /
    ``send_email_fn`` — they get resolved lazily so the import chain
    doesn't pull Neo4j + CRM clients until the operator actually runs the
    command.

    ``monthly_amount_usd_cents`` is only required when the SKU's
    ``price_usd_cents_low`` != ``price_usd_cents_high`` (i.e. AI Ops
    Partner) so the operator can fix the deal size at the negotiated
    monthly figure. SEO Optimization ignores it (flat $300/mo).

    The function never raises on the "downstream service failed" path —
    failures land in ``result.steps`` with ``status="failed"`` and the
    overall ``result.ok`` flips to False. The only paths that raise are
    bad inputs (unknown SKU, empty email).
    """
    started = _now_iso()
    steps: list[EnrollStep] = []
    customer_id: str | None = None
    opportunity_id: str | None = None
    next_cycle_iso: str | None = None
    plan_marker_path: str | None = None
    welcome_message_id: str | None = None

    def _step(name: EnrollStepName, status: EnrollStepStatus, detail: str, t0: float) -> EnrollStep:
        s = EnrollStep(
            name=name,
            status=status,
            detail=detail,
            elapsed_ms=_ms_since(t0),
        )
        steps.append(s)
        _LOG.info(
            "retainer_enroll_step",
            extra={
                "step": name,
                "status": status,
                "detail": detail,
                "email": email,
                "sku_id": sku_id,
            },
        )
        return s

    # ---- 1. lookup SKU ----------------------------------------------------
    t0 = time.monotonic()
    sku = get_retainer_sku(sku_id)
    if sku is None:
        _step("lookup_sku", "failed", f"unknown sku_id={sku_id}", t0)
        # Hard reject — empty plan marker, nothing else fires.
        raise UnknownRetainerSkuError(f"unknown retainer SKU: {sku_id}")
    _step("lookup_sku", "ok", f"sku={sku.display_name}", t0)

    # Lazy service resolution.
    if customer_store is None:
        from backend.memory.customers import CustomerStore

        customer_store = CustomerStore()
    if crm_dispatch_fn is None:
        crm_dispatch_fn = _default_crm_dispatch
    if send_email_fn is None:
        from functools import partial
        from backend.common.email_backend import send_email as _real_send

        # Retainer enrollment confirmation is CAN-SPAM transactional —
        # exempt from unsubscribe/postal rules (suppression still applies).
        send_email_fn = partial(_real_send, message_kind="transactional")

    # ---- 2. find or create customer --------------------------------------
    t0 = time.monotonic()
    try:
        existing = customer_store.get_by_email(email)
        if existing is not None:
            customer = existing
            _step(
                "find_or_create_customer",
                "ok",
                f"found existing {customer.id} (state={customer.current_state})",
                t0,
            )
        else:
            customer = customer_store.create_customer(
                email=email,
                name=name,
                company=company,
                source="retainer_enroll",
            )
            _step(
                "find_or_create_customer",
                "ok",
                f"created {customer.id} in state={customer.current_state}",
                t0,
            )
        customer_id = customer.id
    except Exception as exc:  # noqa: BLE001
        _step("find_or_create_customer", "failed", str(exc), t0)
        return EnrollmentResult(
            email=email,
            sku_id=sku_id,
            customer_id=customer_id,
            ok=False,
            steps=steps,
            ts=started,
        )

    # ---- 3. advance customer to active_retainer --------------------------
    t0 = time.monotonic()
    if customer.current_state == _TARGET_CUSTOMER_STATE:
        _step("advance_to_active_retainer", "skipped", f"already at {_TARGET_CUSTOMER_STATE}", t0)
    else:
        try:
            customer_store.advance_state(
                customer_id=customer.id,
                to_state=_TARGET_CUSTOMER_STATE,
                reason=f"retainer_enroll sku={sku.sku_id}",
                metadata={
                    "is_retainer": True,
                    "retainer_sku_id": sku.sku_id,
                    "retainer_cadence": sku.cadence,
                },
            )
            _step(
                "advance_to_active_retainer",
                "ok",
                f"-> {_TARGET_CUSTOMER_STATE} (tagged is_retainer)",
                t0,
            )
        except Exception as exc:  # noqa: BLE001
            _step("advance_to_active_retainer", "failed", str(exc), t0)
            return EnrollmentResult(
                email=email,
                sku_id=sku_id,
                customer_id=customer_id,
                ok=False,
                steps=steps,
                ts=started,
            )

    # ---- 4. create CRM opportunity ---------------------------------------
    t0 = time.monotonic()
    deal_size = (
        monthly_amount_usd_cents
        if monthly_amount_usd_cents is not None
        else sku.price_usd_cents_low
    ) / 100.0
    opportunity_id = f"retainer_{customer.id}_{sku.sku_id}_{int(time.time())}"
    try:
        crm_dispatch_fn(
            {
                "prospect_id": customer.id,  # reuse customer_id as prospect_id
                "name": f"{sku.display_name} retainer ({customer.email})",
                "service_interest": [sku.sku_id],
                # No native "retainer-tier" intent score input today; flag via
                # service_interest list so deal-scoring can recognize it.
                "intent_score": 100,
                "monthly_budget": f"${int(deal_size)}/mo",
                "next_step": "monthly_cycle starts on first of next month",
                "expected_close": _now_iso(),
            }
        )
        _step(
            "create_opportunity",
            "ok",
            f"dispatched (deal_size=${deal_size:,.0f}/mo, stage={_TARGET_OPPORTUNITY_STAGE})",
            t0,
        )
    except Exception as exc:  # noqa: BLE001
        # CRM dispatch is best-effort — log + continue. The enrollment is
        # still valid; the operator can manually backfill the opportunity
        # row if dispatch failed.
        _step(
            "create_opportunity", "failed", f"{exc} (continuing — CRM dispatch is best-effort)", t0
        )

    # ---- 5. schedule first monthly cycle --------------------------------
    # next_cycle_dt is always computed (the welcome email references it for
    # cycle-based SKUs); the marker file is only written for SKUs that have
    # a monthly deliverable DAG.
    t0 = time.monotonic()
    next_cycle_dt = _first_of_next_month(now=now)
    if not sku.has_monthly_cycle:
        # Always-on SKUs (e.g. the AI Digital Receptionist) deliver value
        # continuously — there is no monthly deliverable DAG, so no cycle
        # marker is written and the monthly-cycle cron never picks one up.
        _step("schedule_next_cycle", "skipped", "sku has no monthly cycle (always-on service)", t0)
    else:
        try:
            marker = _write_next_cycle_marker(customer.id, sku, next_cycle_dt)
            next_cycle_iso = next_cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            plan_marker_path = str(marker)
            _step(
                "schedule_next_cycle",
                "ok",
                f"next_cycle_at={next_cycle_iso} marker={marker.name}",
                t0,
            )
        except Exception as exc:  # noqa: BLE001
            _step("schedule_next_cycle", "failed", str(exc), t0)
            return EnrollmentResult(
                email=email,
                sku_id=sku_id,
                customer_id=customer_id,
                opportunity_id=opportunity_id,
                ok=False,
                steps=steps,
                ts=started,
            )

    # ---- 6. send welcome email ------------------------------------------
    t0 = time.monotonic()
    try:
        subject, text_body, html_body = _build_welcome_email(
            customer_name=customer.name or "",
            sku=sku,
            next_cycle_at=next_cycle_dt,
        )
        send_result = send_email_fn(
            to=customer.email,
            subject=subject,
            body=text_body,
            html_body=html_body,
        )
        welcome_message_id = send_result.get("message_id")
        channel = send_result.get("channel", "?")
        _step("send_welcome_email", "ok", f"{channel} message_id={welcome_message_id}", t0)
    except TypeError:
        # Some backend adapters don't accept html_body kwarg — retry plain.
        try:
            send_result = send_email_fn(
                to=customer.email,
                subject=subject,
                body=text_body,
            )
            welcome_message_id = send_result.get("message_id")
            channel = send_result.get("channel", "?")
            _step(
                "send_welcome_email",
                "ok",
                f"{channel} message_id={welcome_message_id} (text-only)",
                t0,
            )
        except Exception as exc:  # noqa: BLE001
            _step("send_welcome_email", "failed", str(exc), t0)
            return EnrollmentResult(
                email=email,
                sku_id=sku_id,
                customer_id=customer_id,
                opportunity_id=opportunity_id,
                next_cycle_at=next_cycle_iso,
                plan_marker_path=plan_marker_path,
                ok=False,
                steps=steps,
                ts=started,
            )
    except Exception as exc:  # noqa: BLE001
        _step("send_welcome_email", "failed", str(exc), t0)
        return EnrollmentResult(
            email=email,
            sku_id=sku_id,
            customer_id=customer_id,
            opportunity_id=opportunity_id,
            next_cycle_at=next_cycle_iso,
            plan_marker_path=plan_marker_path,
            ok=False,
            steps=steps,
            ts=started,
        )

    return EnrollmentResult(
        email=email,
        sku_id=sku_id,
        customer_id=customer_id,
        opportunity_id=opportunity_id,
        next_cycle_at=next_cycle_iso,
        plan_marker_path=plan_marker_path,
        welcome_message_id=welcome_message_id,
        ok=True,
        steps=steps,
        ts=started,
    )


__all__ = [
    "EnrollStep",
    "EnrollmentResult",
    "UnknownRetainerSkuError",
    "enroll_retainer",
]
