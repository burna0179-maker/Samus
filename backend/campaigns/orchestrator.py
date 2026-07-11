"""Campaign lifecycle driver — turns a declarative template into a live run.

Composes every other module in this package:

  templates + graph  -> the declarative structure
  executor + dispatch -> node execution across the signed workcell boundary
  approval_gate       -> pause/resume at gated nodes
  audit + metrics     -> observability of every transition
  store               -> durable run state

The orchestrator NEVER hard-codes a client or a vertical: it drives whatever
graph the template describes. The Sample School run is therefore a
config instance, not custom code.
"""
from __future__ import annotations

import logging
from typing import Any

from .approval_gate import APPROVAL_KIND, pending_by_severity
from .audit import CampaignAuditLedger, get_campaign_ledger
from .dispatch import default_dispatcher
from .executor import Dispatcher, NodeExecutor
from .graph import CampaignGraph
from .metrics import record_run, set_approval_queue
from .models import (
    CampaignInstance,
    CampaignRun,
    CampaignState,
    CampaignTemplate,
    NodeStatus,
)
from .store import CampaignRunStore
from .templates import load_template_by_id

from backend.common import approvals

_LOG = logging.getLogger("samus.campaigns.orchestrator")


class CampaignError(ValueError):
    """Raised for an invalid lifecycle operation (bad inputs, unknown run)."""


class CampaignOrchestrator:
    """Create + drive campaign runs from declarative templates."""

    def __init__(
        self,
        *,
        store: CampaignRunStore | None = None,
        ledger: CampaignAuditLedger | None = None,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self._store = store or CampaignRunStore()
        self._ledger = ledger or get_campaign_ledger()
        self._dispatcher = dispatcher or default_dispatcher(self._ledger)
        self._executor = NodeExecutor(self._dispatcher, self._ledger)

    # -- lifecycle: create ----------------------------------------------

    def create_campaign(
        self, instance: CampaignInstance, template: CampaignTemplate | None = None
    ) -> CampaignRun:
        """Bind a client instance to a template and produce a READY run."""
        template = template or load_template_by_id(instance.template_id)
        effective_inputs = {
            **template.defaults,
            **instance.inputs,
            "client_id": instance.client_id,
            "campaign_id": instance.campaign_id,
        }
        missing = [
            r
            for r in template.required_inputs
            if effective_inputs.get(r) in (None, "")
        ]
        if missing:
            raise CampaignError(f"missing required inputs: {missing}")

        # Validate the graph is well-formed before we ever mark it ready.
        CampaignGraph(template)

        channels = [c.model_dump() for c in (instance.channels or template.channels)]
        audience = (instance.audience or template.audience)
        vertical_rules = template.vertical_rules

        run = CampaignRun(
            campaign_id=instance.campaign_id,
            client_id=instance.client_id,
            template_id=template.template_id,
            vertical=instance.vertical or template.vertical,
            state=CampaignState.READY,
            context={
                "inputs": effective_inputs,
                "channels": channels,
                "audience": audience.model_dump() if audience else {},
                "vertical_rules": vertical_rules.model_dump() if vertical_rules else None,
                "kpi_targets": dict(instance.kpi_targets),
            },
        )
        self._store.save(run)
        self._ledger.emit_lifecycle_event(
            campaign_id=run.campaign_id, client_id=run.client_id,
            from_state=CampaignState.DRAFT.value, to_state=CampaignState.READY.value,
            reason="created",
        )
        record_run(run.client_id, run.template_id, run.state.value)
        return run

    # -- lifecycle: start / advance -------------------------------------

    def start(self, campaign_id: str) -> CampaignRun:
        """Move a READY/PAUSED run to RUNNING and drive it as far as it can go."""
        run = self._require_run(campaign_id)
        if run.state in (CampaignState.COMPLETED, CampaignState.CANCELLED):
            return run
        template = load_template_by_id(run.template_id)
        self._transition(run, CampaignState.RUNNING, "started")
        return self._drive(run, template)

    def advance(self, campaign_id: str) -> CampaignRun:
        """Continue a run: clear any now-satisfied gates and drive again.

        Resumes from RUNNING / READY / PAUSED / NEEDS_APPROVAL — so an operator
        who approved gates out-of-band (directly in the HOTL queue) can call
        ``/advance`` to pick the campaign back up. Terminal states
        (COMPLETED / FAILED / CANCELLED / BLOCKED) are returned unchanged.
        """
        run = self._require_run(campaign_id)
        resumable = (
            CampaignState.RUNNING,
            CampaignState.READY,
            CampaignState.PAUSED,
            CampaignState.NEEDS_APPROVAL,
        )
        if run.state not in resumable:
            return run
        template = load_template_by_id(run.template_id)
        self._reconcile_approvals(run)
        if run.state != CampaignState.RUNNING:
            self._transition(run, CampaignState.RUNNING, "advanced")
        return self._drive(run, template)

    def _reconcile_approvals(self, run: CampaignRun) -> None:
        """Un-gate any needs-approval node whose approval is now satisfied."""
        from .approval_gate import is_satisfied

        for node_id in list(run.needs_approval_nodes):
            step = run.step_results.get(node_id)
            if step and is_satisfied(step.approval_id):
                run.needs_approval_nodes.remove(node_id)

    def _drive(self, run: CampaignRun, template: CampaignTemplate) -> CampaignRun:
        graph = CampaignGraph(template)
        node_map = template.node_map()
        total = len(node_map)
        # Bounded outer loop: each pass either completes >=1 node or converges.
        for _ in range(total + 1):
            exclude = (
                set(run.completed_nodes)
                | set(run.failed_nodes)
                | set(run.blocked_nodes)
                | set(run.needs_approval_nodes)
            )
            ready = graph.ready_nodes(
                set(run.completed_nodes),
                context={"kpi": run.kpis_updated, "context": run.context},
                exclude=exclude,
            )
            if not ready:
                break
            before = len(run.completed_nodes)
            for node_id in ready:
                self._executor.execute(node_map[node_id], run)
                self._store.save(run)
            if len(run.completed_nodes) == before:
                # No progress this pass — the remainder is gated/failed/blocked.
                break

        self._settle_state(run, total)
        self._store.save(run)
        self._refresh_approval_gauge(run)
        record_run(run.client_id, run.template_id, run.state.value)
        return run

    # -- lifecycle: approval / resume -----------------------------------

    def approve_node(
        self,
        campaign_id: str,
        *,
        node_id: str | None = None,
        approval_id: str | None = None,
        decision: str = "approved",
        decided_by: str = "operator",
    ) -> CampaignRun:
        """Decide a node's approval and resume (or block) the campaign."""
        run = self._require_run(campaign_id)
        approval_id = approval_id or self._approval_for_node(run, node_id)
        if not approval_id:
            raise CampaignError("no pending approval found for that node/campaign")

        row = approvals.decide_approval(approval_id, decision, decided_by=decided_by)
        if row is None:
            raise CampaignError("approval could not be decided (unknown/expired)")

        target_node = node_id or (row.get("payload") or {}).get("node_id")
        if row.get("status") == approvals.STATUS_APPROVED:
            # Clear the gate so the node is re-scheduled on the next drive.
            if target_node in run.needs_approval_nodes:
                run.needs_approval_nodes.remove(target_node)
            self._transition(run, CampaignState.RUNNING, f"approved:{target_node}")
            self._store.save(run)
            template = load_template_by_id(run.template_id)
            return self._drive(run, template)

        # rejected -> the gated node is blocked; the campaign stalls there.
        if target_node and target_node not in run.blocked_nodes:
            run.blocked_nodes.append(target_node)
        if target_node in run.needs_approval_nodes:
            run.needs_approval_nodes.remove(target_node)
        step = run.step_results.get(target_node) if target_node else None
        if step:
            step.status = NodeStatus.BLOCKED
            step.error_summary = "approval_rejected"
        self._settle_state(run, len(load_template_by_id(run.template_id).nodes))
        self._store.save(run)
        self._refresh_approval_gauge(run)
        return run

    def cancel(self, campaign_id: str, reason: str = "operator_cancelled") -> CampaignRun:
        run = self._require_run(campaign_id)
        self._transition(run, CampaignState.CANCELLED, reason)
        self._store.save(run)
        record_run(run.client_id, run.template_id, run.state.value)
        return run

    # -- reads -----------------------------------------------------------

    def get_run(self, campaign_id: str) -> CampaignRun | None:
        return self._store.get(campaign_id)

    def timeline(self, campaign_id: str) -> list[dict[str, Any]]:
        return self._ledger.events_for(campaign_id)

    @property
    def ledger(self) -> CampaignAuditLedger:
        return self._ledger

    # -- internals -------------------------------------------------------

    def _require_run(self, campaign_id: str) -> CampaignRun:
        run = self._store.get(campaign_id)
        if run is None:
            raise CampaignError(f"unknown campaign: {campaign_id}")
        return run

    def _approval_for_node(self, run: CampaignRun, node_id: str | None) -> str | None:
        if node_id:
            step = run.step_results.get(node_id)
            return step.approval_id if step else None
        # No node given -> the single oldest pending approval for this campaign.
        for approval_id in run.approvals_required:
            row = approvals.get_approval(approval_id)
            if (
                row
                and row.get("status") == approvals.STATUS_PENDING
                and row.get("kind") == APPROVAL_KIND
            ):
                return approval_id
        return None

    def _transition(self, run: CampaignRun, to_state: CampaignState, reason: str) -> None:
        from_state = run.state.value
        run.state = to_state
        run.touch()
        self._ledger.emit_lifecycle_event(
            campaign_id=run.campaign_id, client_id=run.client_id,
            from_state=from_state, to_state=to_state.value, reason=reason,
        )

    def _settle_state(self, run: CampaignRun, total: int) -> None:
        """Compute the terminal/paused state after a drive pass."""
        if run.state == CampaignState.CANCELLED:
            return
        run.current_node = None
        if len(set(run.completed_nodes)) >= total:
            self._transition(run, CampaignState.COMPLETED, "all_nodes_completed")
        elif run.failed_nodes:
            self._transition(run, CampaignState.FAILED, "node_failure")
        elif run.blocked_nodes:
            self._transition(run, CampaignState.BLOCKED, "stalled")
        elif run.needs_approval_nodes:
            self._transition(run, CampaignState.NEEDS_APPROVAL, "awaiting_approval")
        else:
            self._transition(run, CampaignState.PAUSED, "no_ready_nodes")

    def _refresh_approval_gauge(self, run: CampaignRun) -> None:
        buckets = pending_by_severity(run)
        for severity in (approvals.SEVERITY_EMERGENCY, approvals.SEVERITY_ROUTINE):
            set_approval_queue(run.client_id, run.campaign_id, severity, buckets.get(severity, 0))


_default: CampaignOrchestrator | None = None


def default_orchestrator() -> CampaignOrchestrator:
    """Process-lazy default orchestrator (production wiring)."""
    global _default
    if _default is None:
        _default = CampaignOrchestrator()
    return _default


def reset_default_orchestrator() -> None:
    global _default
    _default = None
