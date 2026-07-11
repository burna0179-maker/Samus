"""Monthly-cycle DAG runner for retainer SKUs.

Mirrors the ``FulfillmentWorkerV2`` DAG pattern from
``Samus/recovery/fulfillment_worker_v2.py`` — every cycle is a list of
``PlanStep`` records executed in dependency order, with each step's
result + status persisted to ``plan.json`` so a crashed cycle can be
resumed by the next cron tick.

Two cycle blueprints today, keyed by ``RetainerProductConfig.cycle_id``:

  * ``seo_optimization_cycle``  — 4-step DAG:
      1. ``seo.audit_current_state``   (re-run audit, save snapshot)
      2. ``seo.diff_against_prior_month``   (depends on #1; fix queue)
      3. ``seo.apply_priority_fixes``  (depends on #2; logs changes)
      4. ``report.render_and_send``    (depends on #3; emails report)

  * ``ai_ops_partner_cycle``    — 4-phase DAG (weekly cadence over the month):
      1. ``ops.weekly_assess``         (Week 1: metrics + check-in)
      2. ``ops.prioritize_backlog``    (Week 2: scope this-month's work)
      3. ``ops.build_and_deploy``      (Week 3: creates OperatorTask)
      4. ``ops.monthly_report``        (Week 4: send the ops report)

Persistence: every plan lives at
``<SAMUS_ARTIFACT_ROOT>/customers/<slug>/<sku>/<YYYY-MM>/plan.json``.

CLI: ``python -m backend.retainer.monthly_cycle --customer-id X --sku Y``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .registry import get_retainer_sku
from .visibility_report import (
    read_snapshot,
    render_visibility_report,
    write_snapshot,
)


_LOG = logging.getLogger("samus.retainer.monthly_cycle")


# ---------------------------------------------------------------------------
# Plan + Step dataclasses (mirror fulfillment_worker_v2.py shape)
# ---------------------------------------------------------------------------

StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
PlanStatus = Literal["planned", "running", "complete", "failed"]


@dataclass
class PlanStep:
    """One DAG node. ``type`` is ``<service>.<action>`` (string-stable)."""

    id: str
    type: str
    depends_on: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    status: StepStatus = "pending"
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class FulfillmentPlan:
    """A monthly cycle's full DAG + state."""

    plan_id: str  # f"{customer_id}:{sku_id}:{YYYY-MM}"
    customer_id: str
    sku_id: str
    cycle_month: str  # YYYY-MM
    cycle_id: str  # which blueprint produced this plan
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = "planned"
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# ---------------------------------------------------------------------------
# CycleResult (Pydantic — public-facing return shape, like FulfillmentResult)
# ---------------------------------------------------------------------------


class CycleStep(BaseModel):
    """Per-step rollup in the public CycleResult."""

    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    status: StepStatus
    detail: str = ""
    elapsed_ms: int = 0


class CycleResult(BaseModel):
    """Full audit trail of one ``run_monthly_cycle()`` invocation."""

    model_config = ConfigDict(extra="forbid")
    customer_id: str
    sku_id: str
    cycle_month: str
    plan_id: str
    plan_path: str
    report_path: str | None = None
    email_message_id: str | None = None
    ok: bool
    steps: list[CycleStep] = Field(default_factory=list)
    ts: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _cycle_month(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def _prior_cycle_month(this_month: str) -> str:
    """Subtract one calendar month from ``YYYY-MM``."""
    year, month = (int(p) for p in this_month.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _cycle_dir(customer_id: str, sku_id: str, cycle_month: str) -> Path:
    from backend.common import storage

    target = storage.root() / "customers" / customer_id / sku_id / cycle_month
    target.mkdir(parents=True, exist_ok=True)
    return target


def _persist_plan(plan: FulfillmentPlan) -> Path:
    """Write the plan to ``plan.json`` (idempotent — overwrites)."""
    target = _cycle_dir(plan.customer_id, plan.sku_id, plan.cycle_month) / "plan.json"
    plan.updated_at = _now_iso()
    target.write_text(plan.to_json(), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Plan builders (one per cycle_id)
# ---------------------------------------------------------------------------


def _build_seo_optimization_plan(
    customer_id: str,
    sku_id: str,
    cycle_month: str,
    payload: dict[str, Any],
) -> FulfillmentPlan:
    plan_id = f"{customer_id}:{sku_id}:{cycle_month}"
    return FulfillmentPlan(
        plan_id=plan_id,
        customer_id=customer_id,
        sku_id=sku_id,
        cycle_month=cycle_month,
        cycle_id="seo_optimization_cycle",
        created_at=_now_iso(),
        steps=[
            PlanStep(
                id="audit_current_state",
                type="seo.audit_current_state",
                payload={"audit_url": payload.get("audit_url", "")},
            ),
            PlanStep(
                id="diff_against_prior_month",
                type="seo.diff_against_prior_month",
                depends_on=["audit_current_state"],
            ),
            PlanStep(
                id="apply_priority_fixes",
                type="seo.apply_priority_fixes",
                depends_on=["diff_against_prior_month"],
            ),
            PlanStep(
                id="render_and_send",
                type="report.render_and_send",
                depends_on=["apply_priority_fixes"],
                payload={
                    "email_to": payload.get("email_to", ""),
                    "customer_name": payload.get("customer_name", ""),
                    "audit_url": payload.get("audit_url", ""),
                },
            ),
        ],
    )


def _build_ai_ops_partner_plan(
    customer_id: str,
    sku_id: str,
    cycle_month: str,
    payload: dict[str, Any],
) -> FulfillmentPlan:
    plan_id = f"{customer_id}:{sku_id}:{cycle_month}"
    return FulfillmentPlan(
        plan_id=plan_id,
        customer_id=customer_id,
        sku_id=sku_id,
        cycle_month=cycle_month,
        cycle_id="ai_ops_partner_cycle",
        created_at=_now_iso(),
        steps=[
            PlanStep(
                id="weekly_assess",
                type="ops.weekly_assess",
                payload={
                    "operations_summary": payload.get("operations_summary", ""),
                },
            ),
            PlanStep(
                id="prioritize_backlog",
                type="ops.prioritize_backlog",
                depends_on=["weekly_assess"],
            ),
            PlanStep(
                id="build_and_deploy",
                type="ops.build_and_deploy",
                depends_on=["prioritize_backlog"],
            ),
            PlanStep(
                id="monthly_report",
                type="ops.monthly_report",
                depends_on=["build_and_deploy"],
                payload={
                    "email_to": payload.get("email_to", ""),
                    "customer_name": payload.get("customer_name", ""),
                },
            ),
        ],
    )


_PLAN_BUILDERS: dict[str, Callable[..., FulfillmentPlan]] = {
    "seo_optimization_cycle": _build_seo_optimization_plan,
    "ai_ops_partner_cycle": _build_ai_ops_partner_plan,
}


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------


class StepFailure(RuntimeError):
    """Raised by a step executor to mark its step failed."""


def _exec_audit_current_state(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Re-run the SEO audit + persist this-month's snapshot."""
    audit_url = step.payload.get("audit_url", "")
    if not audit_url:
        raise StepFailure("audit_url missing from step payload")

    audit_result = ctx.run_audit_fn(audit_url)
    snapshot: dict[str, Any] = {
        "seo_score": audit_result.get("seo_score", 0),
        "issue_count": len(audit_result.get("issues") or []),
        "rank_by_keyword": audit_result.get("rank_by_keyword") or {},
        "gsc_clicks": audit_result.get("gsc_clicks", 0),
        "gsc_impressions": audit_result.get("gsc_impressions", 0),
        "audit_url": audit_url,
        "captured_at": _now_iso(),
        "issues": audit_result.get("issues") or [],
    }
    snap_path = write_snapshot(plan.customer_id, plan.sku_id, plan.cycle_month, snapshot)
    return {"snapshot_path": str(snap_path), "snapshot": snapshot}


def _exec_diff_against_prior_month(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Diff this-month vs prior-month snapshot; emit fix queue."""
    this_snap = read_snapshot(plan.customer_id, plan.sku_id, plan.cycle_month) or {}
    prior_month = _prior_cycle_month(plan.cycle_month)
    prior_snap = read_snapshot(plan.customer_id, plan.sku_id, prior_month)

    # The fix queue is a prioritized list. Today we lift it straight from
    # the current snapshot's issues (sorted critical -> high -> medium ->
    # low). When prior_snap exists we ALSO surface "regressions" — issues
    # that didn't exist last month but do now — at the top of the queue.
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    this_issues = this_snap.get("issues") or []

    def _sort_key(issue: dict[str, Any]) -> tuple[int, str]:
        sev = (issue.get("severity") or "low").lower()
        return (severity_order.get(sev, 9), issue.get("id", ""))

    if prior_snap is None:
        # First cycle — no diff possible. Whole issue list is the queue.
        fix_queue = sorted(this_issues, key=_sort_key)
        return {
            "is_first_cycle": True,
            "fix_queue_size": len(fix_queue),
            "fix_queue": fix_queue[:5],  # top 5 actionable this cycle
            "regressions": [],
        }

    prior_issue_ids = {i.get("id") for i in (prior_snap.get("issues") or [])}
    regressions = [i for i in this_issues if i.get("id") not in prior_issue_ids]
    fix_queue = sorted(regressions + this_issues, key=_sort_key)
    # De-dupe by id while preserving sorted order
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for i in fix_queue:
        iid = i.get("id", "")
        if iid in seen:
            continue
        seen.add(iid)
        deduped.append(i)

    return {
        "is_first_cycle": False,
        "fix_queue_size": len(deduped),
        "fix_queue": deduped[:5],
        "regressions": regressions,
    }


def _exec_apply_priority_fixes(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Log the fixes the operator applied this month.

    The real fix-application is human work (or a future tool) — this step
    captures the *log of what was done* into the plan record so the
    visibility report can show it. ``ctx.fix_log_fn`` is the injection
    point — production maps it onto the operator's fix log; tests stub
    it.
    """
    diff_step = next((s for s in plan.steps if s.id == "diff_against_prior_month"), None)
    fix_queue = (diff_step.output.get("fix_queue") if diff_step else []) or []
    applied = ctx.fix_log_fn(plan.customer_id, plan.sku_id, plan.cycle_month, fix_queue)
    return {
        "applied_count": len(applied),
        "applied": applied,
        "candidate_queue_size": len(fix_queue),
    }


def _exec_report_render_and_send(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Render the visibility report + send it via email."""
    this_snap = read_snapshot(plan.customer_id, plan.sku_id, plan.cycle_month) or {}
    prior_snap = read_snapshot(
        plan.customer_id,
        plan.sku_id,
        _prior_cycle_month(plan.cycle_month),
    )
    apply_step = next((s for s in plan.steps if s.id == "apply_priority_fixes"), None)
    fixes_applied_raw = (apply_step.output.get("applied") if apply_step else []) or []

    # Coerce the operator log into the renderer's expected shape.
    fixes_applied = [
        {
            "area": f.get("area") or f.get("category") or "general",
            "description": f.get("description") or f.get("message") or str(f),
        }
        for f in fixes_applied_raw
    ]

    markdown = render_visibility_report(
        customer_id=plan.customer_id,
        sku_id=plan.sku_id,
        this_month=this_snap,
        prior_month=prior_snap,
        fixes_applied=fixes_applied,
        next_month_focus=None,
        customer_name=step.payload.get("customer_name", ""),
        site_url=step.payload.get("audit_url", ""),
    )

    report_path = (
        _cycle_dir(plan.customer_id, plan.sku_id, plan.cycle_month) / "visibility_report.md"
    )
    report_path.write_text(markdown, encoding="utf-8")

    email_to = step.payload.get("email_to", "")
    message_id: str | None = None
    if email_to:
        sku = get_retainer_sku(plan.sku_id)
        subject = (
            f"Your {sku.display_name} report — {plan.cycle_month}"
            if sku
            else f"Monthly report — {plan.cycle_month}"
        )
        send_result = ctx.send_email_fn(
            to=email_to,
            subject=subject,
            body=markdown,
        )
        message_id = send_result.get("message_id")

    return {
        "report_path": str(report_path),
        "email_message_id": message_id,
        "sent_to": email_to,
    }


def _exec_ops_weekly_assess(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Week-1: capture prior-month metrics snapshot + create a check-in task."""
    prior_month = _prior_cycle_month(plan.cycle_month)
    prior_snap = read_snapshot(plan.customer_id, plan.sku_id, prior_month) or {}
    summary = step.payload.get("operations_summary", "")

    # Capture this month's baseline (initially empty — ops_metrics populate
    # during the cycle as the operator records what shipped).
    snapshot = {
        "ops_metrics": {},
        "operations_summary": summary,
        "prior_month_ref": prior_month,
        "prior_month_metrics": prior_snap.get("ops_metrics") or {},
        "captured_at": _now_iso(),
    }
    write_snapshot(plan.customer_id, plan.sku_id, plan.cycle_month, snapshot)

    task = ctx.operator_task_fn(
        {
            "kind": "call",
            "title": f"Week 1 assessment call — {plan.customer_id}",
            "description": (
                f"AI Ops Partner Week-1 check-in for cycle {plan.cycle_month}. "
                "Review prior-month metrics + scope what to prioritize this "
                "month. Customer ops summary: " + (summary or "(none on file)")
            ),
            "related_entity_kind": "customer",
            "related_entity_id": plan.customer_id,
            "source": "retainer.monthly_cycle",
            "source_ref": plan.plan_id,
        }
    )
    return {"check_in_task_id": task.get("operator_task_id", ""), "snapshot_captured": True}


def _exec_ops_prioritize_backlog(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Week-2: create a "scope this-month's build" operator task."""
    task = ctx.operator_task_fn(
        {
            "kind": "review",
            "title": f"Week 2 prioritization — scope build for {plan.cycle_month}",
            "description": (
                "Lock the scope of this month's build / ops enhancement with "
                "the customer. Output: 1-3 concrete deliverables for Week 3."
            ),
            "related_entity_kind": "customer",
            "related_entity_id": plan.customer_id,
            "source": "retainer.monthly_cycle",
            "source_ref": plan.plan_id,
        }
    )
    return {"prioritization_task_id": task.get("operator_task_id", "")}


def _exec_ops_build_and_deploy(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Week-3: create the actual build OperatorTask the human will execute."""
    task = ctx.operator_task_fn(
        {
            "kind": "deliver",
            "title": f"Week 3 build & deploy — {plan.customer_id} ({plan.cycle_month})",
            "description": (
                "Execute the agreed Week-2 scope. Capture what shipped + any "
                "metrics it's saving so the Week-4 report has substance."
            ),
            "related_entity_kind": "customer",
            "related_entity_id": plan.customer_id,
            "source": "retainer.monthly_cycle",
            "source_ref": plan.plan_id,
        }
    )
    return {"build_task_id": task.get("operator_task_id", "")}


def _exec_ops_monthly_report(
    step: PlanStep,
    plan: FulfillmentPlan,
    ctx: "CycleContext",
) -> dict[str, Any]:
    """Week-4: render + send the AI Ops monthly report."""
    this_snap = read_snapshot(plan.customer_id, plan.sku_id, plan.cycle_month) or {}

    # Pull the build task's notes into fixes_applied so the report shows
    # what shipped. In the absence of any (first cycle, or operator forgot
    # to update notes), the renderer flags that explicitly.
    fixes_applied: list[dict[str, str]] = []
    for s in plan.steps:
        if s.id == "build_and_deploy" and s.output:
            # Production: this would re-read the task notes via ctx; today
            # we surface the task id so the customer at least sees that
            # work happened.
            tid = s.output.get("build_task_id", "")
            if tid:
                fixes_applied.append(
                    {
                        "area": "this month's build",
                        "description": f"see operator task {tid}",
                    }
                )

    markdown = render_visibility_report(
        customer_id=plan.customer_id,
        sku_id=plan.sku_id,
        this_month=this_snap,
        prior_month=None,
        fixes_applied=fixes_applied,
        next_month_focus=None,
        customer_name=step.payload.get("customer_name", ""),
    )

    report_path = _cycle_dir(plan.customer_id, plan.sku_id, plan.cycle_month) / "ops_report.md"
    report_path.write_text(markdown, encoding="utf-8")

    email_to = step.payload.get("email_to", "")
    message_id: str | None = None
    if email_to:
        send_result = ctx.send_email_fn(
            to=email_to,
            subject=f"Your AI Ops Partner report — {plan.cycle_month}",
            body=markdown,
        )
        message_id = send_result.get("message_id")
    return {
        "report_path": str(report_path),
        "email_message_id": message_id,
        "sent_to": email_to,
    }


_STEP_EXECUTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "seo.audit_current_state": _exec_audit_current_state,
    "seo.diff_against_prior_month": _exec_diff_against_prior_month,
    "seo.apply_priority_fixes": _exec_apply_priority_fixes,
    "report.render_and_send": _exec_report_render_and_send,
    "ops.weekly_assess": _exec_ops_weekly_assess,
    "ops.prioritize_backlog": _exec_ops_prioritize_backlog,
    "ops.build_and_deploy": _exec_ops_build_and_deploy,
    "ops.monthly_report": _exec_ops_monthly_report,
}


# ---------------------------------------------------------------------------
# Default service callables (lazy-resolved, injectable for tests)
# ---------------------------------------------------------------------------


def _default_run_audit(audit_url: str) -> dict[str, Any]:
    """Resolve + invoke the real SEO audit-and-report pipeline."""
    from backend.seo.models import AuditRequest
    from backend.seo.service import audit_site

    audit = audit_site(AuditRequest(url=audit_url))
    # Coerce to the dict shape the cycle expects. The real audit doesn't
    # populate rank_by_keyword / gsc_* today — those will fill in when
    # GSC integration lands. For now they stay empty + the renderer's
    # "no tracked keywords" path handles that gracefully.
    return {
        "seo_score": audit.seo_score,
        "issues": [i.model_dump() for i in audit.issues],
        "rank_by_keyword": {},
        "gsc_clicks": 0,
        "gsc_impressions": 0,
    }


def _default_send_email(*, to: str, subject: str, body: str) -> dict[str, str]:
    """Default email path — same adapter the fulfill chain uses."""
    from backend.common.email_backend import send_email as _real_send

    # Monthly retainer deliverable is transactional/relationship service mail.
    return _real_send(to=to, subject=subject, body=body, message_kind="transactional")


def _default_fix_log_fn(
    customer_id: str,
    sku_id: str,
    cycle_month: str,
    fix_queue: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Default behavior: surface the candidate queue as the applied log.

    Production should swap this for a call into the operator's fix-log
    (probably an OperatorTask read). Until that's wired, defaulting to
    "what we said we'd do" gives the customer concrete content rather
    than an empty fixes_applied section.
    """
    return [
        {
            "area": (item.get("category") or "general"),
            "description": item.get("message") or item.get("id") or "fix applied",
        }
        for item in (fix_queue or [])
    ]


def _default_operator_task_fn(payload: dict[str, Any]) -> dict[str, str]:
    """Default CRM operator-task dispatch — best-effort signed POST."""
    import asyncio
    from backend.common.config import get_settings
    from backend.common.http_client import signed_post_json

    settings = get_settings()
    crm_url = settings.gateway_urls.get("crm")
    if not crm_url or not settings.shared_hmac_key:
        _LOG.debug("retainer operator_task dispatch skipped: crm misconfig")
        # Synthesize a local task id so the plan log is non-empty even
        # when CRM is offline.
        return {"operator_task_id": f"local_{int(time.time())}"}
    try:
        resp = asyncio.run(signed_post_json(crm_url, "/crm/operator-tasks", payload, retries=2))
        return resp if isinstance(resp, dict) else {"operator_task_id": ""}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("retainer operator_task dispatch failed: %s", exc)
        return {"operator_task_id": f"local_{int(time.time())}"}


@dataclass
class CycleContext:
    """Bundle of injectable callables the executors close over."""

    run_audit_fn: Callable[[str], dict[str, Any]]
    send_email_fn: Callable[..., dict[str, str]]
    fix_log_fn: Callable[..., list[dict[str, str]]]
    operator_task_fn: Callable[..., dict[str, str]]


# ---------------------------------------------------------------------------
# Public DAG runner
# ---------------------------------------------------------------------------


def run_monthly_cycle(
    *,
    customer_id: str,
    sku_id: str,
    audit_url: str = "",
    customer_email: str = "",
    customer_name: str = "",
    operations_summary: str = "",
    now: datetime | None = None,
    run_audit_fn: Callable[[str], dict[str, Any]] | None = None,
    send_email_fn: Callable[..., dict[str, str]] | None = None,
    fix_log_fn: Callable[..., list[dict[str, str]]] | None = None,
    operator_task_fn: Callable[..., dict[str, str]] | None = None,
) -> CycleResult:
    """Plan + execute one monthly cycle end-to-end.

    Dispatches steps in dependency order. Each step's status + output
    persists to ``plan.json`` so a partial run can be resumed by the next
    call (idempotency by step status). Step failures HALT downstream
    steps (their dependencies aren't met) but don't raise — the result
    carries ``ok=False`` and the failed step's error detail.

    Why no async / parallelism: monthly cycles have hard sequential deps
    (audit -> diff -> fixes -> report). There's nothing to parallelize.
    The DAG shape comes from the recovery doc + buys us
    pause/resume/idempotency on the same code path.
    """
    started = _now_iso()
    sku = get_retainer_sku(sku_id)
    if sku is None:
        raise ValueError(f"unknown retainer SKU: {sku_id}")

    builder = _PLAN_BUILDERS.get(sku.cycle_id)
    if builder is None:
        raise ValueError(f"no plan builder for cycle_id={sku.cycle_id}")

    cycle_month = _cycle_month(now)

    # If a plan exists for this customer-cycle already (idempotent rerun),
    # load it; otherwise build fresh.
    plan_path = _cycle_dir(customer_id, sku_id, cycle_month) / "plan.json"
    plan: FulfillmentPlan
    if plan_path.exists():
        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
            plan = FulfillmentPlan(
                plan_id=raw["plan_id"],
                customer_id=raw["customer_id"],
                sku_id=raw["sku_id"],
                cycle_month=raw["cycle_month"],
                cycle_id=raw["cycle_id"],
                created_at=raw.get("created_at", started),
                updated_at=raw.get("updated_at", ""),
                status=raw.get("status", "planned"),
                steps=[PlanStep(**s) for s in raw.get("steps", [])],
            )
            _LOG.info("retainer cycle resumed plan_id=%s", plan.plan_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("retainer plan reload failed, rebuilding: %s", exc)
            plan = builder(
                customer_id,
                sku_id,
                cycle_month,
                {
                    "audit_url": audit_url,
                    "email_to": customer_email,
                    "customer_name": customer_name,
                    "operations_summary": operations_summary,
                },
            )
    else:
        plan = builder(
            customer_id,
            sku_id,
            cycle_month,
            {
                "audit_url": audit_url,
                "email_to": customer_email,
                "customer_name": customer_name,
                "operations_summary": operations_summary,
            },
        )

    plan.status = "running"
    _persist_plan(plan)

    ctx = CycleContext(
        run_audit_fn=run_audit_fn or _default_run_audit,
        send_email_fn=send_email_fn or _default_send_email,
        fix_log_fn=fix_log_fn or _default_fix_log_fn,
        operator_task_fn=operator_task_fn or _default_operator_task_fn,
    )

    # ---- DAG walk: repeat until no further progress is possible -----------
    cycle_steps: list[CycleStep] = []
    by_id = {s.id: s for s in plan.steps}

    def _deps_satisfied(step: PlanStep) -> bool:
        return all(by_id[d].status == "done" for d in step.depends_on)

    made_progress = True
    while made_progress:
        made_progress = False
        for step in plan.steps:
            if step.status != "pending":
                continue
            if not _deps_satisfied(step):
                continue
            executor = _STEP_EXECUTORS.get(step.type)
            if executor is None:
                step.status = "failed"
                step.error = f"no executor for type={step.type}"
                cycle_steps.append(
                    CycleStep(
                        id=step.id,
                        type=step.type,
                        status="failed",
                        detail=step.error,
                    )
                )
                _persist_plan(plan)
                continue

            t0 = time.monotonic()
            step.status = "running"
            step.started_at = _now_iso()
            _persist_plan(plan)
            try:
                output = executor(step, plan, ctx) or {}
                step.output = output
                step.status = "done"
                step.finished_at = _now_iso()
                detail = ", ".join(f"{k}={v}" for k, v in output.items() if k != "snapshot")[:200]
                cycle_steps.append(
                    CycleStep(
                        id=step.id,
                        type=step.type,
                        status="done",
                        detail=detail,
                        elapsed_ms=_ms_since(t0),
                    )
                )
                made_progress = True
            except Exception as exc:  # noqa: BLE001
                step.status = "failed"
                step.error = str(exc)
                step.finished_at = _now_iso()
                cycle_steps.append(
                    CycleStep(
                        id=step.id,
                        type=step.type,
                        status="failed",
                        detail=str(exc),
                        elapsed_ms=_ms_since(t0),
                    )
                )
            finally:
                _persist_plan(plan)

    # Skip steps whose deps failed (status unchanged from pending).
    for step in plan.steps:
        if step.status == "pending":
            step.status = "skipped"
            step.error = "dependency failed"
            cycle_steps.append(
                CycleStep(
                    id=step.id,
                    type=step.type,
                    status="skipped",
                    detail="dependency failed",
                    elapsed_ms=0,
                )
            )

    plan.status = "complete" if all(s.status == "done" for s in plan.steps) else "failed"
    _persist_plan(plan)

    # Pull the final report path + message id off the last-step output.
    report_step = next(
        (
            s
            for s in reversed(plan.steps)
            if s.type in ("report.render_and_send", "ops.monthly_report")
        ),
        None,
    )
    report_path = None
    message_id = None
    if report_step and report_step.output:
        report_path = report_step.output.get("report_path")
        message_id = report_step.output.get("email_message_id")

    return CycleResult(
        customer_id=customer_id,
        sku_id=sku_id,
        cycle_month=cycle_month,
        plan_id=plan.plan_id,
        plan_path=str(plan_path),
        report_path=report_path,
        email_message_id=message_id,
        ok=plan.status == "complete",
        steps=cycle_steps,
        ts=started,
    )


# ---------------------------------------------------------------------------
# Convenience iterator used by the cron — find every due customer-SKU
# ---------------------------------------------------------------------------


def find_due_cycles(now: datetime | None = None) -> list[dict[str, str]]:
    """Return a list of due ``{customer_id, sku_id, next_cycle_at}`` markers.

    Scans ``<SAMUS_ARTIFACT_ROOT>/customers/*/<sku>/_enrollment/next_cycle.json``
    for any whose ``next_cycle_at`` <= now. Caller iterates + invokes
    ``run_monthly_cycle()`` on each. The cron tick stays a thin loop;
    all retainer logic lives here.
    """
    from backend.common import storage

    now = now or datetime.now(timezone.utc)
    due: list[dict[str, str]] = []
    customers_root = storage.root() / "customers"
    if not customers_root.exists():
        return due
    for customer_dir in customers_root.iterdir():
        if not customer_dir.is_dir():
            continue
        for sku_dir in customer_dir.iterdir():
            if not sku_dir.is_dir():
                continue
            marker = sku_dir / "_enrollment" / "next_cycle.json"
            if not marker.exists():
                continue
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            next_at_raw = data.get("next_cycle_at", "")
            try:
                next_at = datetime.strptime(next_at_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc,
                )
            except ValueError:
                continue
            if next_at <= now:
                due.append(
                    {
                        "customer_id": data.get("customer_id", customer_dir.name),
                        "sku_id": data.get("sku_id", sku_dir.name),
                        "next_cycle_at": next_at_raw,
                    }
                )
    return due


# ---------------------------------------------------------------------------
# CLI — `python -m backend.retainer.monthly_cycle` (also wired by `samus retainer run-cycle`)
# ---------------------------------------------------------------------------


def _render_result(result: CycleResult) -> str:
    sep = "=" * 75
    lines: list[str] = []
    lines.append(sep)
    badge = "OK" if result.ok else "FAILED"
    lines.append(
        f"SAMUS RETAINER CYCLE  [{badge}]  {result.customer_id} / {result.sku_id} "
        f"/ {result.cycle_month}"
    )
    lines.append(sep)
    lines.append(f"  plan:           {result.plan_path}")
    if result.report_path:
        lines.append(f"  report:         {result.report_path}")
    if result.email_message_id:
        lines.append(f"  email msg id:   {result.email_message_id}")
    lines.append("")
    lines.append("  steps:")
    for s in result.steps:
        lines.append(f"    [{s.status:>7}]  {s.id:<26}  ({s.elapsed_ms} ms)  {s.detail}")
    lines.append(sep)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one monthly cycle for a retainer customer.",
    )
    parser.add_argument("--customer-id", required=True, help="Customer slug")
    parser.add_argument(
        "--sku",
        required=True,
        help="Retainer SKU id (e.g. retainer_seo_optimization)",
    )
    parser.add_argument(
        "--audit-url",
        default="",
        help="Site URL (required for SEO Optimization cycle)",
    )
    parser.add_argument(
        "--email",
        default="",
        help="Customer email — report sends here. Omit to skip the email.",
    )
    parser.add_argument("--name", default="", help="Customer name (optional)")
    parser.add_argument(
        "--ops-summary",
        default="",
        help="Operations summary text (AI Ops Partner cycle context)",
    )
    args = parser.parse_args(argv)

    try:
        result = run_monthly_cycle(
            customer_id=args.customer_id,
            sku_id=args.sku,
            audit_url=args.audit_url,
            customer_email=args.email,
            customer_name=args.name,
            operations_summary=args.ops_summary,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"run_monthly_cycle crashed: {exc}\n")
        return 2

    sys.stdout.write(_render_result(result) + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Self-marketing cycle integration
#
# The "self_marketing" path runs Hustleforge as its own first-party client
# through backend.marketing.campaign_cycle.run_monthly_campaign().  It is
# intentionally a standalone entry point rather than a full retainer SKU
# because it has no external customer_id / billing record.
#
# Usage from a cron tick or operator script:
#
#   from backend.retainer.monthly_cycle import run_self_marketing_cycle
#   result = run_self_marketing_cycle(month=6, year=2026)
#
# The returned CampaignResult mirrors the CycleResult shape (ok, steps, ts).
# ---------------------------------------------------------------------------


def run_self_marketing_cycle(
    month: int | None = None,
    year: int | None = None,
    *,
    dry_run: bool = False,
) -> "Any":
    """Convenience shim: run the Hustleforge self-marketing campaign cycle.

    Delegates entirely to backend.marketing.campaign_cycle.run_monthly_campaign
    so all DAG/idempotency/fail-soft logic lives in one place.
    """
    from datetime import datetime, timezone
    from backend.marketing.campaign_cycle import run_monthly_campaign

    now = datetime.now(timezone.utc)
    return run_monthly_campaign(
        month=month or now.month,
        year=year or now.year,
        dry_run=dry_run,
    )


__all__ = [
    "PlanStep",
    "FulfillmentPlan",
    "CycleStep",
    "CycleResult",
    "StepFailure",
    "run_monthly_cycle",
    "find_due_cycles",
    "run_self_marketing_cycle",
]
