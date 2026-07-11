"""Execute one campaign node — the mechanical heart of the orchestrator.

For a single node this module:

1. resolves the effective approval level + audit severity;
2. FAIL-CLOSED refuses a high-risk node whose approval resolves to ``none``
   (deliverable §8 — high-risk actions must never auto-run);
3. pauses at an approval gate when one is required and not yet satisfied;
4. projects the run context into a dispatch payload (input mapping);
5. dispatches the capability — locally for the ``campaigns`` workcell, otherwise
   through the SIGNED GATEWAY seam — under the node's bounded retry policy;
6. applies rollback behaviour on ultimate failure;
7. folds the result back into KPIs / artifacts / context (output mapping);
8. emits a tamper-evident audit event AND Prometheus metrics for every attempt.

Dispatch is injected (:class:`Dispatcher` protocol) so it is deterministic in
tests and always crosses the real trust boundary in production.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Protocol

from backend.common.correlation import ensure_trace_id

from . import approval_gate, kpi, metrics
from .audit import CampaignAuditLedger, hash_payload
from .models import (
    ApprovalLevel,
    CampaignArtifact,
    CampaignNode,
    CampaignRun,
    CampaignStepResult,
    NodeStatus,
    RollbackMode,
)
from .registry import LOCAL_WORKCELL, is_high_risk

_LOG = logging.getLogger("samus.campaigns.executor")


class DispatchError(RuntimeError):
    """A node's capability dispatch failed (network, upstream 5xx, refusal)."""


class Dispatcher(Protocol):
    """Routes a node's capability to where it actually runs."""

    def dispatch(
        self, *, node: CampaignNode, payload: dict[str, Any], run: CampaignRun
    ) -> dict[str, Any]:
        ...


def resolve_inputs(node: CampaignNode, run: CampaignRun) -> dict[str, Any]:
    """Project the run context into the dispatch payload (input mapping).

    Each ``input_mapping`` value is either a literal, a ``$``-prefixed
    reference into the run context (``$inputs.school_name``, ``$kpi.page_views``,
    ``$context.foo``), or a ``dict`` whose values are themselves resolved the
    same way (recursively, so a nested dict may contain further ``$``-refs or
    nested dicts). Missing references resolve to ``None`` rather than raising
    so a template author gets a predictable payload. ``campaign_id`` /
    ``client_id`` are always injected.
    """
    ctx = {
        "inputs": run.context.get("inputs", {}),
        "kpi": run.kpis_updated,
        "context": run.context,
        "channels": run.context.get("channels", []),
        "audience": run.context.get("audience", {}),
    }
    payload: dict[str, Any] = {}
    for key, spec in node.input_mapping.items():
        payload[key] = _resolve_spec(spec, ctx)
    payload.setdefault("campaign_id", run.campaign_id)
    payload.setdefault("client_id", run.client_id)
    payload.setdefault("node_id", node.id)
    return payload


def _resolve_spec(spec: Any, ctx: dict[str, Any]) -> Any:
    """Resolve a single ``input_mapping`` value (literal, ``$``-ref, or dict).

    A ``dict`` value is projected key-by-key through this same function, so
    ``$``-refs nested arbitrarily deep inside a dict literal are resolved
    against the run context instead of passing through unresolved. Flat
    strings/literals behave exactly as before — this only adds a branch for
    the ``dict`` case.
    """
    if isinstance(spec, str) and spec.startswith("$"):
        return _resolve_ref(spec, ctx)
    if isinstance(spec, dict):
        return {k: _resolve_spec(v, ctx) for k, v in spec.items()}
    return spec


def _resolve_ref(ref: str, ctx: dict[str, Any]) -> Any:
    cur: Any = ctx
    for part in ref[1:].split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class NodeExecutor:
    """Executes individual nodes with audit + metrics + approval gating."""

    def __init__(self, dispatcher: Dispatcher, ledger: CampaignAuditLedger) -> None:
        self._dispatcher = dispatcher
        self._ledger = ledger

    def execute(self, node: CampaignNode, run: CampaignRun) -> CampaignStepResult:
        trace_id = ensure_trace_id()
        step = run.step_results.get(node.id) or CampaignStepResult(node_id=node.id)
        step.trace_id = trace_id
        run.step_results[node.id] = step

        severity = approval_gate.resolve_severity(node)

        # (2) Fail-closed: a high-risk node whose author DECLARED no approval is
        # a config error — refuse rather than silently gate or (worse) perform
        # an un-gated public effect. We key off the *declared* approval, not the
        # resolved level, precisely so the misconfiguration surfaces instead of
        # being masked by the node-type default raising the floor.
        if is_high_risk(node.type) and node.approval_required == ApprovalLevel.NONE:
            return self._reject_unsafe(node, run, step, severity, trace_id)

        level = approval_gate.resolve_level(node, _vertical_rules(run))

        # (3) Approval gate — pause instead of executing.
        if level != ApprovalLevel.NONE:
            approval_id, approved = approval_gate.ensure(run, node, level)
            step.approval_id = approval_id
            if approval_id and approval_id not in run.approvals_required:
                run.approvals_required.append(approval_id)
            if not approved:
                return self._pause_for_approval(node, run, step, severity, level, trace_id)

        # (4)-(6) dispatch with retry + rollback.
        return self._run_dispatch(node, run, step, severity, level, trace_id)

    # -- outcome branches -------------------------------------------------

    def _reject_unsafe(self, node, run, step, severity, trace_id) -> CampaignStepResult:
        step.status = NodeStatus.BLOCKED
        step.error_summary = "high_risk_node_requires_approval"
        _mark(run.blocked_nodes, node.id)
        metrics.record_node_failure(run.client_id, run.template_id, node.id, "unsafe_unapproved")
        event_id = self._ledger.emit_node_event(
            campaign_id=run.campaign_id, client_id=run.client_id, node_id=node.id,
            node_type=node.type, target_workcell=node.target_workcell,
            capability=node.capability, status="blocked", severity=severity.value,
            approval_state="missing_required", error_summary=step.error_summary,
            trace_id=trace_id,
        )
        run.audit_events.append(event_id)
        _LOG.warning(
            "campaigns.unsafe_node_refused campaign=%s node=%s type=%s — high-risk "
            "with approval_required=none; refusing to auto-run",
            run.campaign_id, node.id, node.type,
        )
        return step

    def _pause_for_approval(self, node, run, step, severity, level, trace_id) -> CampaignStepResult:
        step.status = NodeStatus.NEEDS_APPROVAL
        _mark(run.needs_approval_nodes, node.id)
        event_id = self._ledger.emit_node_event(
            campaign_id=run.campaign_id, client_id=run.client_id, node_id=node.id,
            node_type=node.type, target_workcell=node.target_workcell,
            capability=node.capability, status="needs_approval", severity=severity.value,
            approval_state=f"pending:{level.value}", trace_id=trace_id,
        )
        run.audit_events.append(event_id)
        return step

    def _run_dispatch(self, node, run, step, severity, level, trace_id) -> CampaignStepResult:
        step.status = NodeStatus.RUNNING
        # A node that was previously paused/blocked is now clearing.
        _unmark(run.needs_approval_nodes, node.id)
        _unmark(run.blocked_nodes, node.id)
        run.current_node = node.id

        payload = resolve_inputs(node, run)
        input_hash = hash_payload(payload)
        approval_state = "approved" if level != ApprovalLevel.NONE else "not_required"

        started = time.time()
        step.started_at = _iso(started)
        result: dict[str, Any] | None = None
        error = ""
        for attempt in range(1, max(1, node.retry.max_attempts) + 1):
            step.attempts = attempt
            try:
                result = self._dispatcher.dispatch(node=node, payload=payload, run=run)
                error = ""
                break
            except Exception as exc:  # noqa: BLE001 — classify + maybe retry
                error = f"{type(exc).__name__}: {exc}"
                if not _should_retry(node, exc) or attempt >= node.retry.max_attempts:
                    break
                _backoff(node, attempt)

        duration_ms = (time.time() - started) * 1000.0
        step.finished_at = _iso(time.time())
        step.duration_ms = duration_ms
        metrics.observe_node_duration(run.client_id, run.template_id, node.id, duration_ms / 1000.0)

        if result is None:
            return self._finish_failure(node, run, step, severity, approval_state,
                                        input_hash, error, trace_id)
        return self._finish_success(node, run, step, severity, approval_state,
                                    input_hash, result, trace_id)

    def _finish_success(self, node, run, step, severity, approval_state,
                        input_hash, result, trace_id) -> CampaignStepResult:
        artifact_refs, kpi_refs = self._apply_outputs(node, run, result)
        step.status = NodeStatus.COMPLETED
        step.output_summary = _summarize(result)
        step.output_hash = hash_payload(result)
        step.artifact_refs = artifact_refs
        step.kpi_refs = kpi_refs
        step.error_summary = ""
        _mark(run.completed_nodes, node.id)
        _unmark(run.failed_nodes, node.id)
        event_id = self._ledger.emit_node_event(
            campaign_id=run.campaign_id, client_id=run.client_id, node_id=node.id,
            node_type=node.type, target_workcell=node.target_workcell,
            capability=node.capability, status="completed", severity=severity.value,
            approval_state=approval_state, input_hash=input_hash,
            output_hash=step.output_hash, duration_ms=step.duration_ms,
            artifact_refs=artifact_refs, kpi_refs=kpi_refs, trace_id=trace_id,
        )
        run.audit_events.append(event_id)
        return step

    def _finish_failure(self, node, run, step, severity, approval_state,
                        input_hash, error, trace_id) -> CampaignStepResult:
        step.status = NodeStatus.FAILED
        step.error_summary = error or "dispatch_failed"
        _mark(run.failed_nodes, node.id)
        metrics.record_node_failure(run.client_id, run.template_id, node.id, "dispatch")
        event_id = self._ledger.emit_node_event(
            campaign_id=run.campaign_id, client_id=run.client_id, node_id=node.id,
            node_type=node.type, target_workcell=node.target_workcell,
            capability=node.capability, status="failed", severity=severity.value,
            approval_state=approval_state, input_hash=input_hash,
            duration_ms=step.duration_ms, error_summary=step.error_summary,
            trace_id=trace_id,
        )
        run.audit_events.append(event_id)
        self._maybe_compensate(node, run)
        return step

    # -- output application ----------------------------------------------

    def _apply_outputs(
        self, node: CampaignNode, run: CampaignRun, result: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        artifact_refs: list[str] = []
        kpi_refs: list[str] = []

        for raw in result.get("artifacts", []) or []:
            artifact = _coerce_artifact(raw, node.id)
            run.artifacts_created.append(artifact)
            artifact_refs.append(artifact.artifact_id)
            metrics.inc_artifacts(run.client_id, run.campaign_id, artifact.type)

        result_kpis = result.get("kpis") or {}
        if isinstance(result_kpis, dict) and result_kpis:
            events = [
                {"kpi": k, "mode": "set", "value": v}
                for k, v in result_kpis.items()
                if kpi.is_known_kpi(k)
            ]
            if events:
                changed = kpi.apply_kpi_events(run, events)
                kpi_refs.extend(changed.keys())

        # output_mapping copies result fields into the run context for
        # downstream nodes / conditional edges. Each value is a dotted path into
        # the node result (e.g. "report.url" -> result["report"]["url"]).
        for ctx_key, result_path in node.output_mapping.items():
            if isinstance(result_path, str):
                run.context[ctx_key] = _resolve_ref("$" + result_path, result)
            else:
                run.context[ctx_key] = result_path

        return artifact_refs, kpi_refs

    def _maybe_compensate(self, node: CampaignNode, run: CampaignRun) -> None:
        if node.rollback.mode != RollbackMode.COMPENSATE:
            return
        cap = node.rollback.compensation_capability
        if not cap:
            return
        try:
            self._dispatcher.dispatch(
                node=node.model_copy(update={"capability": cap}),
                payload={"campaign_id": run.campaign_id, "client_id": run.client_id,
                         "node_id": node.id, "compensate": True},
                run=run,
            )
        except Exception as exc:  # noqa: BLE001 — compensation is best-effort
            _LOG.warning("campaigns.compensation_failed node=%s: %s", node.id, exc)


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def _vertical_rules(run: CampaignRun):
    vr = run.context.get("vertical_rules")
    if vr is None:
        return None
    from .models import CampaignVerticalRules

    try:
        return CampaignVerticalRules.model_validate(vr)
    except Exception:  # noqa: BLE001
        return None


def _should_retry(node: CampaignNode, exc: Exception) -> bool:
    allow = node.retry.retry_on
    if not allow:
        return True
    name = type(exc).__name__
    return any(token in name or token in str(exc) for token in allow)


def _backoff(node: CampaignNode, attempt: int) -> None:
    delay = node.retry.backoff_seconds * (node.retry.backoff_multiplier ** (attempt - 1))
    if delay > 0:
        time.sleep(min(delay, 30.0))


def _coerce_artifact(raw: Any, node_id: str) -> CampaignArtifact:
    if isinstance(raw, CampaignArtifact):
        return raw
    data = dict(raw) if isinstance(raw, dict) else {"ref": str(raw)}
    data.setdefault("artifact_id", uuid.uuid4().hex)
    data.setdefault("type", data.get("type", "generic"))
    data.setdefault("node_id", node_id)
    if "ref" in data and not data.get("content_hash"):
        data["content_hash"] = hash_payload(data["ref"])
    return CampaignArtifact.model_validate(data)


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    """A small, PII-lean summary of a node result for the step record."""
    keep = {}
    for k in ("status", "summary", "count", "url", "report_ref"):
        if k in result:
            keep[k] = result[k]
    keep["keys"] = sorted(result.keys())
    return keep


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _mark(bucket: list[str], node_id: str) -> None:
    if node_id not in bucket:
        bucket.append(node_id)


def _unmark(bucket: list[str], node_id: str) -> None:
    if node_id in bucket:
        bucket.remove(node_id)
