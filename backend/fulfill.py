"""Customer-fulfillment orchestrator CLI.

Single command that takes a freshly-closed sale (customer email + their site URL)
through the post-payment delivery loop:

  1. Find or create customer in memory (idempotent on email).
  2. Advance state to 'in_delivery'.
  3. Run SEO audit_and_report against the URL -> writes the markdown
     deliverable to <SAMUS_ARTIFACT_ROOT>/customers/<slug>/seo_report.md.
  4. Email the report to the customer via SES (unless --no-send).
  5. Advance state to 'delivered'.

Each step is logged. Failures abort the sequence without rolling forward the
state — operator can re-run with the same args (idempotent at each layer).

Run from the Samus venv:

    python -m backend.fulfill --email customer@example.com --url https://acme-plumbing.example.com

The companion PowerShell wrapper (``scripts/Run-Fulfill.ps1``) pulls
HivemindPassword (Neo4j) + AWS keys + Stripe key from DPAPI before invoking.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


_LOG = logging.getLogger("samus.fulfill")


# ---------------------------------------------------------------------------
# Step / result models
# ---------------------------------------------------------------------------

StepName = Literal[
    "find_or_create_customer",
    "advance_to_in_delivery",
    "audit_and_report",
    "send_email",
    "advance_to_delivered",
]
StepStatus = Literal["ok", "skipped", "failed"]


class FulfillStep(BaseModel):
    """One step's outcome inside a fulfillment run."""

    model_config = ConfigDict(extra="forbid")

    name: StepName
    status: StepStatus
    detail: str = ""  # one-line human summary
    elapsed_ms: int = 0


class FulfillmentResult(BaseModel):
    """Full audit trail of a fulfill_customer() invocation."""

    model_config = ConfigDict(extra="forbid")

    email: str
    audit_url: str
    customer_id: str | None = None
    prior_state: str | None = None
    final_state: str | None = None
    report_path: str | None = None
    email_message_id: str | None = None
    ok: bool
    steps: list[FulfillStep] = Field(default_factory=list)
    ts: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_DEFAULT_EMAIL_SUBJECT = "Your SEO audit + content drafts"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    import time

    return int((time.monotonic() - start) * 1000)


def _email_body(report_text: str, customer_name: str = "") -> str:
    """Plain-text email body: short cover + the full markdown report inline."""
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    return (
        f"{greeting}\n\n"
        f"Your SEO audit is ready. The full report (audit findings, prioritized\n"
        f"recommendations, and content drafts) is below.\n\n"
        f"Reply to this email with any questions.\n\n"
        f"-- Hustleforge\n\n"
        f"{'=' * 72}\n\n"
        f"{report_text}\n"
    )


def fulfill_customer(
    *,
    email: str,
    audit_url: str,
    name: str = "",
    company: str = "",
    target_keywords: list[str] | None = None,
    tone: str = "professional",
    send_email: bool = True,
    customer_store: Any = None,
    audit_and_report_fn: Any = None,
    send_email_fn: Any = None,
) -> FulfillmentResult:
    """Drive the post-payment fulfillment loop end-to-end.

    The three injectable callables let tests swap in fakes:
      - ``customer_store``        : a CustomerStore-like instance
      - ``audit_and_report_fn``   : callable(AuditRequest, **kw) -> dict
      - ``send_email_fn``         : callable(to, subject, body) -> dict

    Production callers omit them; this function resolves the real services
    at call time so the import chain doesn't trigger Neo4j / Stripe lookups
    until the operator actually runs the command.

    Email adapter selection (production path, no injection):
      Priority 1: backend.common.email_backend (env-var selector; routes via
        SendGrid when EMAIL_BACKEND=sendgrid, the platform default).
      Fallback: backend.outreach.ses_adapter.send_email_via_ses — used when
        email_backend module is unavailable or EMAIL_BACKEND=ses is set.
    Tests still inject their own send_email_fn.
    """
    import time

    started = _now_iso()
    steps: list[FulfillStep] = []
    customer_id: str | None = None
    prior_state: str | None = None
    final_state: str | None = None
    report_path: str | None = None
    email_message_id: str | None = None

    def _step(name: StepName, status: StepStatus, detail: str, t0: float) -> FulfillStep:
        s = FulfillStep(name=name, status=status, detail=detail, elapsed_ms=_ms_since(t0))
        steps.append(s)
        _LOG.info(
            "fulfill_step",
            extra={
                "step": name,
                "status": status,
                "detail": detail,
                "email": email,
                "url": audit_url,
            },
        )
        return s

    # Lazy service resolution (so test fixtures can inject before import).
    if customer_store is None:
        from backend.memory.customers import CustomerStore

        customer_store = CustomerStore()
    if audit_and_report_fn is None:
        from backend.seo.service import audit_and_report as _real_audit

        audit_and_report_fn = _real_audit
    if send_email_fn is None:
        # Try the common adapter selector first (EMAIL_BACKEND env-var driven).
        # SendGrid is the platform default (EMAIL_BACKEND=sendgrid or unset).
        # Fall back to direct SES adapter if the common module isn't available
        # (e.g. pure-SES deployments where email_backend module hasn't been wired).
        try:
            from backend.common.email_backend import send_email as _real_send
        except ImportError:
            # Direct SES adapter has no message_kind/guard concept — use as-is.
            from backend.outreach.ses_adapter import send_email_via_ses as _real_send

            send_email_fn = _real_send
        else:
            from functools import partial

            # Fulfillment delivery is transactional — exempt from the
            # ComplianceGuard unsubscribe/postal rules in enforce mode.
            send_email_fn = partial(_real_send, message_kind="transactional")

    # ---- 1. find or create the customer ----------------------------------
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
                source="fulfill_cli",
            )
            _step(
                "find_or_create_customer",
                "ok",
                f"created {customer.id} in state={customer.current_state}",
                t0,
            )
        customer_id = customer.id
        prior_state = customer.current_state
        final_state = customer.current_state
    except Exception as exc:
        _step("find_or_create_customer", "failed", str(exc), t0)
        return FulfillmentResult(
            email=email,
            audit_url=audit_url,
            customer_id=customer_id,
            prior_state=prior_state,
            final_state=final_state,
            ok=False,
            steps=steps,
            ts=started,
        )

    # ---- 2. advance to in_delivery (if not already past it) --------------
    t0 = time.monotonic()
    if customer.current_state in ("delivered", "renewed", "churned"):
        _step(
            "advance_to_in_delivery",
            "skipped",
            f"current_state={customer.current_state} already past in_delivery",
            t0,
        )
    elif customer.current_state == "in_delivery":
        _step("advance_to_in_delivery", "skipped", "already in_delivery", t0)
    else:
        try:
            event = customer_store.advance_state(
                customer_id=customer.id,
                to_state="in_delivery",
                reason="fulfill_cli started delivery",
            )
            final_state = event.to_state
            _step("advance_to_in_delivery", "ok", f"{event.from_state} -> in_delivery", t0)
        except Exception as exc:
            _step("advance_to_in_delivery", "failed", str(exc), t0)
            return FulfillmentResult(
                email=email,
                audit_url=audit_url,
                customer_id=customer_id,
                prior_state=prior_state,
                final_state=final_state,
                ok=False,
                steps=steps,
                ts=started,
            )

    # ---- 3. SEO audit + report ------------------------------------------
    t0 = time.monotonic()
    try:
        from backend.seo.models import AuditRequest

        req = AuditRequest(url=audit_url, keywords=target_keywords or [], industry="")
        result = audit_and_report_fn(
            req,
            target_keywords=target_keywords,
            tone=tone,
            customer_label=customer.id,  # slug under data/customers/
        )
        report_path = result.get("report_path")
        seo_score = (result.get("audit") or {}).get("seo_score")
        _step("audit_and_report", "ok", f"report written to {report_path} (score={seo_score})", t0)
    except Exception as exc:
        _step("audit_and_report", "failed", str(exc), t0)
        return FulfillmentResult(
            email=email,
            audit_url=audit_url,
            customer_id=customer_id,
            prior_state=prior_state,
            final_state=final_state,
            report_path=report_path,
            ok=False,
            steps=steps,
            ts=started,
        )

    # ---- 4. email the report (optional) ---------------------------------
    if send_email:
        t0 = time.monotonic()
        try:
            report_text = Path(report_path).read_text(encoding="utf-8")
            body = _email_body(report_text, customer_name=customer.name)
            send_result = send_email_fn(
                to=customer.email,
                subject=_DEFAULT_EMAIL_SUBJECT,
                body=body,
            )
            email_message_id = send_result.get("message_id")
            channel = send_result.get("channel", "?")
            _step("send_email", "ok", f"{channel} message_id={email_message_id}", t0)
        except Exception as exc:
            # Email failure leaves state at in_delivery — operator can retry
            # the send_email step alone via outreach workcell, then advance
            # to delivered manually. Better than auto-advancing on a failed
            # send.
            _step("send_email", "failed", str(exc), t0)
            return FulfillmentResult(
                email=email,
                audit_url=audit_url,
                customer_id=customer_id,
                prior_state=prior_state,
                final_state=final_state,
                report_path=report_path,
                ok=False,
                steps=steps,
                ts=started,
            )
    else:
        _step("send_email", "skipped", "--no-send", 0.0)

    # ---- 5. advance to delivered ----------------------------------------
    t0 = time.monotonic()
    try:
        reason = "fulfill_cli delivered report"
        if send_email:
            reason += f" (email message_id={email_message_id})"
        event = customer_store.advance_state(
            customer_id=customer.id,
            to_state="delivered",
            reason=reason,
        )
        final_state = event.to_state
        _step("advance_to_delivered", "ok", f"{event.from_state} -> delivered", t0)
    except Exception as exc:
        _step("advance_to_delivered", "failed", str(exc), t0)
        return FulfillmentResult(
            email=email,
            audit_url=audit_url,
            customer_id=customer_id,
            prior_state=prior_state,
            final_state=final_state,
            report_path=report_path,
            email_message_id=email_message_id,
            ok=False,
            steps=steps,
            ts=started,
        )

    # ---- 6. queue the audit->monthly upsell nurture sequence -------------
    # Best-effort: a queue write failure must not unwind a successful
    # delivery. The hourly daemon thread in samus-finance drains the queue
    # on cadence (D+5, D+12, D+30 from now).
    try:
        from datetime import datetime, timezone
        from backend.finance.upsell_queue import enqueue_upsell

        enqueue_upsell(
            customer_id=customer.id,
            customer_email=email,
            source_offer_code="seo_audit",  # only audit upsell wired today
            delivered_at=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("upsell enqueue failed for %s: %s", email, exc)

    return FulfillmentResult(
        email=email,
        audit_url=audit_url,
        customer_id=customer_id,
        prior_state=prior_state,
        final_state=final_state,
        report_path=report_path,
        email_message_id=email_message_id,
        ok=True,
        steps=steps,
        ts=started,
    )


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def _render_result(result: FulfillmentResult) -> str:
    sep = "=" * 75
    lines: list[str] = []
    lines.append(sep)
    badge = "OK" if result.ok else "FAILED"
    lines.append(f"SAMUS FULFILL  [{badge}]  {result.email}  ->  {result.audit_url}")
    lines.append(sep)
    if result.customer_id:
        lines.append(f"  customer:        {result.customer_id}")
        lines.append(f"  state:           {result.prior_state} -> {result.final_state}")
    if result.report_path:
        lines.append(f"  report:          {result.report_path}")
    if result.email_message_id:
        lines.append(f"  email msg id:    {result.email_message_id}")
    lines.append("")
    lines.append("  steps:")
    for s in result.steps:
        lines.append(f"    [{s.status:>7}]  {s.name:<26}  ({s.elapsed_ms} ms)  {s.detail}")
    lines.append(sep)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive the post-payment fulfillment loop for one customer.",
    )
    parser.add_argument("--email", required=True, help="Customer email address")
    parser.add_argument("--url", required=True, help="Customer's site URL to audit")
    parser.add_argument("--name", default="", help="Customer's name (optional)")
    parser.add_argument("--company", default="", help="Customer's company name (optional)")
    parser.add_argument(
        "--keywords",
        default="",
        help="Comma-separated target keywords for the audit (optional)",
    )
    parser.add_argument(
        "--tone",
        default="professional",
        help="Content draft tone: professional | friendly | technical",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Skip the email step (still writes the report + advances state to delivered)",
    )
    args = parser.parse_args(argv)

    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    try:
        result = fulfill_customer(
            email=args.email,
            audit_url=args.url,
            name=args.name,
            company=args.company,
            target_keywords=keywords or None,
            tone=args.tone,
            send_email=not args.no_send,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"fulfill_customer crashed: {exc}\n")
        return 2

    sys.stdout.write(_render_result(result) + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
