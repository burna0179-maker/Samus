"""Weekly campaign report — assembled from run state + the audit ledger.

Deliverable §10: the report is *derived*, never hand-assembled. It reads the
:class:`CampaignRun` (KPIs, artifacts, node buckets, approvals) and the campaign
audit ledger (per-node timeline) and produces a structured report artifact with
an executive summary, KPI table, completed/blocked/next actions, top channels,
weak points, recommendations, outstanding approval requests, and artifact links.

The rendered report is written to a file under the state root and returned as a
:class:`CampaignArtifact` whose ``ref`` points at that file — only the reference
travels through the run/ledger, never the full body.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.common import approvals
from backend.common.state_paths import state_path

from .audit import CampaignAuditLedger
from .kpi import INITIAL_KPIS
from .models import CampaignArtifact, CampaignRun

_REPORTS_DIR_ENV = "SAMUS_CAMPAIGN_REPORTS_DIR"


def _reports_dir() -> Path:
    override = os.getenv(_REPORTS_DIR_ENV, "").strip()
    return Path(override) if override else state_path("campaigns", "reports")


def build_report(run: CampaignRun, ledger: CampaignAuditLedger) -> dict[str, Any]:
    """Assemble the report body (pure — no file I/O)."""
    events = ledger.events_for(run.campaign_id)
    completed = list(run.completed_nodes)
    blocked = list(run.blocked_nodes)
    failed = list(run.failed_nodes)
    awaiting = list(run.needs_approval_nodes)

    kpi_rows = []
    for key, value in sorted(run.kpis_updated.items()):
        definition = INITIAL_KPIS.get(key)
        kpi_rows.append(
            {
                "kpi": key,
                "label": definition.label if definition else key,
                "value": value,
                "target": definition.target if definition else None,
                "unit": definition.unit if definition else "count",
            }
        )

    top_channels = _top_channels(run)
    approval_requests = _open_approvals(run)

    return {
        "campaign_id": run.campaign_id,
        "client_id": run.client_id,
        "template_id": run.template_id,
        "vertical": run.vertical,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": run.state.value,
        "executive_summary": _exec_summary(run, completed, blocked, failed, awaiting),
        "kpi_table": kpi_rows,
        "completed_actions": completed,
        "blocked_actions": blocked + failed,
        "next_actions": _next_actions(run),
        "top_performing_channels": top_channels,
        "weak_points": _weak_points(run, blocked, failed, awaiting),
        "recommendations": _recommendations(run, blocked, failed, awaiting),
        "approval_requests": approval_requests,
        "artifact_links": [
            {"artifact_id": a.artifact_id, "type": a.type, "ref": a.ref, "title": a.title}
            for a in run.artifacts_created
        ],
        "audit_event_count": len(events),
    }


def generate_weekly_report(run: CampaignRun, ledger: CampaignAuditLedger) -> CampaignArtifact:
    """Build the report, persist it, and return the artifact reference."""
    body = build_report(run, ledger)
    reports = _reports_dir()
    reports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{run.campaign_id}-weekly-{stamp}.json"
    path = reports / filename
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")

    return CampaignArtifact(
        artifact_id=uuid.uuid4().hex,
        type="weekly_report",
        ref=str(path),
        node_id="weekly_report",
        title=f"Weekly report — {run.campaign_id} ({stamp})",
        metadata={"kpi_count": len(body["kpi_table"]), "state": run.state.value},
    )


# --- section builders ------------------------------------------------------


def _exec_summary(run, completed, blocked, failed, awaiting) -> str:
    return (
        f"Campaign {run.campaign_id} ({run.vertical}) is {run.state.value}. "
        f"{len(completed)} stage(s) completed, {len(blocked) + len(failed)} "
        f"blocked/failed, {len(awaiting)} awaiting approval. "
        f"{len(run.artifacts_created)} artifact(s) produced across "
        f"{len(run.kpis_updated)} tracked KPI(s)."
    )


def _next_actions(run: CampaignRun) -> list[str]:
    actions: list[str] = []
    for node_id in run.needs_approval_nodes:
        actions.append(f"Approve gated node '{node_id}' to resume the campaign")
    for node_id in run.failed_nodes:
        actions.append(f"Investigate + retry failed node '{node_id}'")
    if not actions and run.state.value == "running":
        actions.append("Continue automated execution of remaining stages")
    return actions


def _top_channels(run: CampaignRun) -> list[dict[str, Any]]:
    channels = run.context.get("channels") or []
    ranked = []
    for ch in channels:
        name = ch.get("name") if isinstance(ch, dict) else str(ch)
        ranked.append({"channel": name, "kind": ch.get("kind") if isinstance(ch, dict) else ""})
    return ranked[:5]


def _weak_points(run, blocked, failed, awaiting) -> list[str]:
    weak: list[str] = []
    if failed:
        weak.append(f"{len(failed)} node(s) failed after retry: {failed}")
    if awaiting:
        weak.append(f"{len(awaiting)} node(s) stalled awaiting approval: {awaiting}")
    for key, value in run.kpis_updated.items():
        definition = INITIAL_KPIS.get(key)
        if definition and definition.target and value < definition.target:
            weak.append(f"KPI '{key}' at {value} below target {definition.target}")
    return weak


def _recommendations(run, blocked, failed, awaiting) -> list[str]:
    recs: list[str] = []
    if awaiting:
        recs.append("Clear the approval queue to unblock downstream stages")
    if failed:
        recs.append("Review failed-node error summaries in the audit timeline")
    if not run.kpis_updated:
        recs.append("Wire funnel analytics so KPI ingestion begins reporting")
    if not recs:
        recs.append("Campaign healthy — maintain current cadence")
    return recs


def _open_approvals(run: CampaignRun) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for approval_id in run.approvals_required:
        row = approvals.get_approval(approval_id)
        if row and row.get("status") == approvals.STATUS_PENDING:
            out.append(
                {
                    "approval_id": approval_id,
                    "node_id": (row.get("payload") or {}).get("node_id"),
                    "severity": row.get("severity"),
                    "level": (row.get("payload") or {}).get("approval_level"),
                    "expires_at": row.get("ttl_expires_at"),
                }
            )
    return out
