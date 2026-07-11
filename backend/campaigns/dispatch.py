"""Node dispatchers — where a campaign node's capability actually runs.

Two implementations behind the :class:`executor.Dispatcher` protocol:

* :class:`GatewayDispatcher` — the production path for any node targeting a
  peer workcell. It wraps the mapped payload in a :class:`TaskEnvelope` and
  POSTs it to the gateway's ``/dispatch/{target}`` seam via the shared
  HMAC-signed client. It NEVER reaches a peer workcell directly — the gateway
  re-signs under its own identity and fans out — so the campaign layer stays
  inside the Samus trust boundary (feedback: no bypassing signed dispatch).

* :class:`LocalCapabilityDispatcher` — runs the ``campaigns`` workcell's own
  capabilities (KPI collection, report generation, KPI ingestion) in-process
  instead of a pointless self-HTTP round trip.

:class:`CompositeDispatcher` routes between them by target workcell.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from backend.common.config import get_settings
from backend.common.http_client import signed_post_json_sync
from backend.common.models import TaskEnvelope

from . import kpi
from .audit import CampaignAuditLedger
from .executor import DispatchError
from .models import CampaignNode, CampaignRun
from .registry import LOCAL_WORKCELL

_LOG = logging.getLogger("samus.campaigns.dispatch")


class GatewayDispatcher:
    """Dispatch a node to a peer workcell through the signed gateway seam."""

    def __init__(self, *, retries: int = 2) -> None:
        self._retries = retries

    def dispatch(
        self, *, node: CampaignNode, payload: dict[str, Any], run: CampaignRun
    ) -> dict[str, Any]:
        settings = get_settings()
        gateway_url = (settings.gateway_urls or {}).get("gateway")
        if not gateway_url:
            raise DispatchError("gateway_url_unset: cannot dispatch campaign node")
        if not getattr(settings, "shared_hmac_key", "") and not getattr(
            settings, "per_service_hmac_keys", None
        ):
            raise DispatchError("hmac_key_unset: refusing to dispatch unsigned")

        envelope = TaskEnvelope(
            task_id=f"{run.campaign_id}:{node.id}:{uuid.uuid4().hex[:8]}",
            payload=payload,
            metadata={
                "action": node.capability,
                "campaign_id": run.campaign_id,
                "client_id": run.client_id,
                "node_id": node.id,
                "node_type": node.type,
            },
        )
        try:
            resp = signed_post_json_sync(
                gateway_url,
                f"/dispatch/{node.target_workcell}",
                envelope.model_dump(),
                retries=self._retries,
            )
        except Exception as exc:  # noqa: BLE001 — normalize to DispatchError
            raise DispatchError(f"gateway_transport_error: {exc}") from exc
        if resp.status_code >= 400:
            raise DispatchError(
                f"gateway_status_{resp.status_code}: "
                f"{resp.text[:200] if hasattr(resp, 'text') else ''}"
            )
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — non-JSON upstream
            return {"status": "ok", "raw": True}
        return body if isinstance(body, dict) else {"status": "ok", "result": body}


class LocalCapabilityDispatcher:
    """Run the ``campaigns`` workcell's own capabilities in-process."""

    def __init__(self, ledger: CampaignAuditLedger) -> None:
        self._ledger = ledger

    def dispatch(
        self, *, node: CampaignNode, payload: dict[str, Any], run: CampaignRun
    ) -> dict[str, Any]:
        cap = node.capability
        if cap == "update_kpis":
            # metrics_collection node: snapshot the current funnel state.
            return {"status": "ok", "summary": "kpi_snapshot", "kpi_snapshot": kpi.snapshot(run)}
        if cap == "ingest_kpi":
            events = payload.get("events") or []
            changed = kpi.apply_kpi_events(run, events)
            return {"status": "ok", "summary": "kpi_ingest", "changed": changed}
        if cap == "generate_report":
            from .report import generate_weekly_report

            artifact = generate_weekly_report(run, self._ledger)
            return {
                "status": "ok",
                "summary": "weekly_report",
                "report_ref": artifact.ref,
                "artifacts": [artifact.model_dump()],
            }
        raise DispatchError(f"unknown_local_capability: {cap}")


class CompositeDispatcher:
    """Route by target workcell: local ``campaigns`` vs signed gateway."""

    def __init__(self, local: LocalCapabilityDispatcher, remote: GatewayDispatcher) -> None:
        self._local = local
        self._remote = remote

    def dispatch(
        self, *, node: CampaignNode, payload: dict[str, Any], run: CampaignRun
    ) -> dict[str, Any]:
        if node.target_workcell == LOCAL_WORKCELL:
            return self._local.dispatch(node=node, payload=payload, run=run)
        return self._remote.dispatch(node=node, payload=payload, run=run)


def default_dispatcher(ledger: CampaignAuditLedger) -> CompositeDispatcher:
    """The production dispatcher: local handlers + signed gateway fan-out."""
    return CompositeDispatcher(LocalCapabilityDispatcher(ledger), GatewayDispatcher())
