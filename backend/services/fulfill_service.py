"""Service-tier fulfillment orchestrator. Mirrors backend/fulfill.py shape; handles scope-confirmation chain."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.common import storage
from backend.services import scope_planner, sla_timer
from backend.services.registry import ServiceSku, get_sku


_LOG = logging.getLogger("samus.services.fulfill")


# ---------------------------------------------------------------------------
# Step models
# ---------------------------------------------------------------------------

StepName = Literal[
    "lookup_sku",
    "find_or_create_customer",
    "advance_to_in_delivery",
    "arm_sla",
    "generate_scope",
    "validate_scope_gates",
    "write_scope_artifact",
    "write_workflow_artifact",
    "send_scope_email",
    "advance_to_scope_confirmed",
]
StepStatus = Literal["ok", "skipped", "failed"]


class FulfillStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StepName
    status: StepStatus
    detail: str = ""
    elapsed_ms: int = 0


class FulfillmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str
    email: str
    customer_id: Optional[str] = None
    prior_state: Optional[str] = None
    final_state: Optional[str] = None
    scope_path: Optional[str] = None
    workflow_path: Optional[str] = None
    email_message_id: Optional[str] = None
    sla_deadline: Optional[str] = None
    out_of_scope_reason: Optional[str] = None
    ok: bool
    steps: list[FulfillStep] = Field(default_factory=list)
    ts: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _scope_artifact_path(customer_slug: str, sku_id: str) -> Path:
    """<SAMUS_ARTIFACT_ROOT>/customers/<slug>/<sku>/scope.md."""
    return storage.root() / "customers" / customer_slug / sku_id / "scope.md"


def _read_delivery_template(sku: ServiceSku) -> str:
    """Best-effort read of the delivery template; returns "" if missing (operator can re-issue)."""
    base = Path(__file__).resolve().parent
    candidate = base / sku.delivery_template_path
    if not candidate.exists():
        _LOG.warning("delivery_template_missing path=%s sku=%s", candidate, sku.sku_id)
        return ""
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError as exc:
        _LOG.warning("delivery_template_read_failed path=%s err=%s", candidate, exc)
        return ""


def _email_body_for_scope(
    sku: ServiceSku, scope_markdown: str, customer_name: str = ""
) -> tuple[str, str]:
    """Plain-text + HTML body for the scope-confirmation email."""
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    intro = (
        f"{greeting}\n\n"
        f"Thanks for purchasing {sku.display_name}. Below is the scope of work — "
        f"please review and reply 'confirm' to start the SLA clock. The full "
        f"document is also saved on file.\n\n"
        f"{'=' * 72}\n\n"
    )
    text = intro + scope_markdown + "\n\n-- Hustleforge\n"
    # Minimal HTML wrapper — pre-formatted so the markdown renders readable.
    safe = scope_markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        "<html><body>"
        f"<p>{greeting}</p>"
        f"<p>Thanks for purchasing <strong>{sku.display_name}</strong>. "
        f"Below is the scope of work — please review and reply <strong>'confirm'</strong> "
        f"to start the SLA clock.</p>"
        "<pre style=\"font-family: Consolas, 'Courier New', monospace; "
        'font-size: 13px; line-height: 1.45; white-space: pre-wrap;">'
        f"{safe}"
        "</pre>"
        "<p>— Hustleforge</p>"
        "</body></html>"
    )
    return text, html


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def fulfill_service(
    *,
    sku_id: str,
    email: str,
    intake_payload: dict[str, Any],
    name: str = "",
    company: str = "",
    send_email: bool = True,
    customer_store: Any = None,
    send_email_fn: Any = None,
) -> FulfillmentResult:
    """Drive the scope-confirmation loop for one service-tier SKU.

    Lifecycle: lookup_sku → find_or_create_customer → advance_to_in_delivery →
    arm_sla → generate_scope → validate_scope_gates (Workflow Rescue only) →
    write_scope_artifact → send_scope_email → advance_to_scope_confirmed.

    Returns a FulfillmentResult with full step trace. Failures short-circuit
    forward state transitions (the customer is left in in_delivery so the
    operator can retry without losing work).
    """
    started = _now_iso()
    steps: list[FulfillStep] = []
    customer_id: Optional[str] = None
    prior_state: Optional[str] = None
    final_state: Optional[str] = None
    scope_path: Optional[str] = None
    workflow_path: Optional[str] = None
    email_message_id: Optional[str] = None
    sla_deadline: Optional[str] = None
    out_of_scope_reason: Optional[str] = None

    def _step(name: StepName, status: StepStatus, detail: str, t0: float) -> None:
        steps.append(FulfillStep(name=name, status=status, detail=detail, elapsed_ms=_ms_since(t0)))
        _LOG.info(
            "fulfill_service_step",
            extra={
                "step": name,
                "status": status,
                "detail": detail,
                "sku_id": sku_id,
                "email": email,
            },
        )

    def _result(ok: bool) -> FulfillmentResult:
        return FulfillmentResult(
            sku_id=sku_id,
            email=email,
            customer_id=customer_id,
            prior_state=prior_state,
            final_state=final_state,
            scope_path=scope_path,
            workflow_path=workflow_path,
            email_message_id=email_message_id,
            sla_deadline=sla_deadline,
            out_of_scope_reason=out_of_scope_reason,
            ok=ok,
            steps=steps,
            ts=started,
        )

    # ---- 1. lookup sku ---------------------------------------------------
    t0 = time.monotonic()
    try:
        sku = get_sku(sku_id)
        _step("lookup_sku", "ok", f"{sku.display_name} (sla={sku.sla_hours}h)", t0)
    except KeyError as exc:
        _step("lookup_sku", "failed", str(exc), t0)
        return _result(ok=False)

    # ---- 2. find or create customer --------------------------------------
    if customer_store is None:
        from backend.memory.customers import CustomerStore

        customer_store = CustomerStore()
    if send_email_fn is None:
        from functools import partial
        from backend.common.email_backend import send_email as _real_send

        # Product fulfillment delivery is CAN-SPAM transactional/relationship
        # mail — tag it so the ComplianceGuard exempts it from the unsubscribe
        # + postal-address rules (suppression check still applies).
        send_email_fn = partial(_real_send, message_kind="transactional")

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
                source="fulfill_service",
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
        return _result(ok=False)

    # ---- 3. advance to in_delivery --------------------------------------
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
                reason=f"fulfill_service[{sku_id}] starting scope chain",
            )
            final_state = event.to_state
            _step("advance_to_in_delivery", "ok", f"{event.from_state} -> in_delivery", t0)
        except Exception as exc:
            _step("advance_to_in_delivery", "failed", str(exc), t0)
            return _result(ok=False)

    # ---- 4. arm SLA timer ------------------------------------------------
    t0 = time.monotonic()
    try:
        rec = sla_timer.arm_sla(
            customer_store=customer_store,
            customer_id=customer.id,
            sku_id=sku.sku_id,
            sla_hours=sku.sla_hours,
        )
        sla_deadline = rec.get("sla_deadline")
        _step("arm_sla", "ok", f"deadline={sla_deadline}", t0)
    except Exception as exc:
        _step("arm_sla", "failed", str(exc), t0)
        return _result(ok=False)

    # ---- 5. generate scope artifact -------------------------------------
    t0 = time.monotonic()
    try:
        # Make sure the intake payload carries the customer's email even if the
        # caller forgot — the scope renderer expects it.
        intake = dict(intake_payload or {})
        intake.setdefault("email", email)
        artifact = scope_planner.generate_scope(intake, sku.sku_id)
        _step(
            "generate_scope",
            "ok",
            f"steps={artifact.estimated_steps} templates={artifact.estimated_templates}",
            t0,
        )
    except Exception as exc:
        _step("generate_scope", "failed", str(exc), t0)
        return _result(ok=False)

    # ---- 6. validate scope gates (Workflow Rescue only) -----------------
    # Constants inlined from recovery/fixed_scope_template_pipeline.py so we don't
    # take a runtime dependency on the recovery module (stripped from some
    # deployment targets). If those constants ever change, update them in lockstep.
    t0 = time.monotonic()
    if sku.scope_gates_enforced:
        MAX_STEPS, MAX_TOOLS, MAX_TEMPLATES = 5, 3, 3
        reason: Optional[str] = None
        if artifact.estimated_steps > MAX_STEPS:
            reason = f"workflow_steps_exceeded: {artifact.estimated_steps} > {MAX_STEPS}"
        elif len(artifact.plan.tools) > MAX_TOOLS:
            reason = f"tools_exceeded: {len(artifact.plan.tools)} > {MAX_TOOLS}"
        elif artifact.estimated_templates > MAX_TEMPLATES:
            reason = f"templates_exceeded: {artifact.estimated_templates} > {MAX_TEMPLATES}"
        if reason:
            artifact.out_of_scope_reason = reason
            out_of_scope_reason = reason
            _step(
                "validate_scope_gates",
                "ok",
                f"OUT_OF_SCOPE: {reason} (continuing — scope doc flags it for operator review)",
                t0,
            )
        else:
            _step(
                "validate_scope_gates",
                "ok",
                f"in_scope (steps={artifact.estimated_steps}, tools={len(artifact.plan.tools)}, "
                f"templates={artifact.estimated_templates})",
                t0,
            )
    else:
        _step(
            "validate_scope_gates", "skipped", f"sku {sku.sku_id} does not enforce scope gates", t0
        )

    # ---- 7. write scope artifact to disk --------------------------------
    t0 = time.monotonic()
    try:
        scope_md = scope_planner.render_scope_markdown(artifact, sku=sku)
        delivery_tmpl = _read_delivery_template(sku)
        if delivery_tmpl:
            scope_md += (
                "\n\n"
                "---\n\n"
                "## Operator delivery playbook (internal — not sent to customer)\n\n"
                + delivery_tmpl
            )
        path = _scope_artifact_path(customer.id, sku.sku_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scope_md, encoding="utf-8")
        scope_path = str(path)
        _step("write_scope_artifact", "ok", f"wrote {len(scope_md)} chars to {path}", t0)
    except Exception as exc:
        _step("write_scope_artifact", "failed", str(exc), t0)
        return _result(ok=False)

    # ---- 7b. generate the runnable n8n workflow deliverable (fail-soft) --
    # Additive: turns the parsed TaskPlan into an importable n8n workflow.json +
    # runbook.md beside the scope doc. MUST NOT break the paid scope flow — any
    # failure is recorded and we continue (the operator can regenerate).
    _WORKFLOW_SKUS = {
        "service_workflow_rescue",
        "service_workflow_buildout",
        "service_ai_ops_partner_build",
    }
    if sku.sku_id in _WORKFLOW_SKUS:
        t0 = time.monotonic()
        try:
            from backend.common.config import get_settings
            from backend.workflow.service import generate_workflow_deliverable

            settings = get_settings()
            if not bool(getattr(settings, "workflow_n8n_deliverable_enabled", True)):
                _step(
                    "write_workflow_artifact",
                    "skipped",
                    "workflow_n8n_deliverable_enabled is off",
                    t0,
                )
            else:
                report = generate_workflow_deliverable(
                    artifact,
                    out_dir=Path(scope_path).parent
                    if scope_path
                    else _scope_artifact_path(customer.id, sku.sku_id).parent,
                    settings=settings,
                    sku=sku,
                )
                workflow_path = report.get("workflow_path")
                _step(
                    "write_workflow_artifact",
                    "ok",
                    f"nodes={report.get('node_count')} valid={report.get('valid')} "
                    f"deploy={report.get('deploy', {}).get('status')}",
                    t0,
                )
        except Exception as exc:  # noqa: BLE001 — additive deliverable, never fatal
            _step("write_workflow_artifact", "failed", f"{type(exc).__name__}: {exc}", t0)
    else:
        t0 = time.monotonic()
        _step("write_workflow_artifact", "skipped", f"sku {sku.sku_id} is not a workflow build", t0)

    # ---- 8. send scope confirmation email -------------------------------
    if send_email:
        t0 = time.monotonic()
        try:
            # Customer-facing email contains scope_markdown only (no operator playbook).
            customer_md = scope_planner.render_scope_markdown(artifact, sku=sku)
            text_body, html_body = _email_body_for_scope(
                sku, customer_md, customer_name=customer.name
            )
            subject = f"Scope confirmation — {sku.display_name}"
            send_result = send_email_fn(
                to=customer.email,
                subject=subject,
                body=text_body,
                html_body=html_body,
            )
            email_message_id = send_result.get("message_id")
            channel = send_result.get("channel", "?")
            _step("send_scope_email", "ok", f"{channel} message_id={email_message_id}", t0)
        except Exception as exc:
            _step("send_scope_email", "failed", str(exc), t0)
            return _result(ok=False)
    else:
        t0 = time.monotonic()
        _step("send_scope_email", "skipped", "send_email=False", t0)

    # ---- 9. advance to scope_confirmed ----------------------------------
    t0 = time.monotonic()
    try:
        reason = f"fulfill_service[{sku_id}] scope email sent (msg_id={email_message_id or 'n/a'})"
        event = customer_store.advance_state(
            customer_id=customer.id,
            to_state="scope_confirmed",
            reason=reason,
        )
        final_state = event.to_state
        _step("advance_to_scope_confirmed", "ok", f"{event.from_state} -> scope_confirmed", t0)
    except Exception as exc:
        _step("advance_to_scope_confirmed", "failed", str(exc), t0)
        return _result(ok=False)

    return _result(ok=True)
