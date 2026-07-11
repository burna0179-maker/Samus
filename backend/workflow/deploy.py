"""Deploy a compiled workflow to an n8n instance via its REST API.

DORMANT + dry-run by default, mirroring ``website/deploy_cloudflare.py``. Does
nothing (returns a dry-run result) unless an operator sets
``workflow_n8n_deploy_enabled`` AND ``workflow_n8n_dry_run=false`` AND provides
``n8n_base_url`` + ``n8n_api_key``. The live path is a single
``POST {base}/api/v1/workflows`` with the ``X-N8N-API-KEY`` header. Returns a
structured dict either way; never raises.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.workflow.models import N8nWorkflow

_LOG = logging.getLogger("samus.workflow.deploy")

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=15.0, pool=5.0)


def _http_post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    """Thin wrapper — the monkeypatch point in tests."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        return client.post(url, json=json, headers=headers)


def deploy_workflow(wf: N8nWorkflow, *, settings) -> dict[str, Any]:
    """Push ``wf`` to the configured n8n instance. Fail-closed; never raises."""
    if not bool(getattr(settings, "workflow_n8n_deploy_enabled", False)):
        return {"status": "disabled", "deployed": False}
    if bool(getattr(settings, "workflow_n8n_dry_run", True)):
        return {"status": "dry_run", "deployed": False}

    base = (getattr(settings, "n8n_base_url", "") or "").strip().rstrip("/")
    api_key = (getattr(settings, "n8n_api_key", "") or "").strip()
    if not base or not api_key:
        return {"status": "no_credentials", "deployed": False}

    # n8n's create endpoint accepts name/nodes/connections/settings (not the
    # read-only fields like 'active'/'id').
    payload = {k: v for k, v in wf.to_dict().items() if k in ("name", "nodes", "connections", "settings")}
    url = f"{base}/api/v1/workflows"
    try:
        resp = _http_post(url, json=payload, headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        _LOG.warning("n8n deploy transport error: %s", exc)
        return {"status": "transport_error", "deployed": False, "error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code not in (200, 201):
        return {"status": f"http_{resp.status_code}", "deployed": False, "error": resp.text[:200]}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    workflow_id = str((body or {}).get("id") or (body or {}).get("data", {}).get("id") or "")
    return {"status": "ok", "deployed": True, "workflow_id": workflow_id}


__all__ = ["deploy_workflow"]
