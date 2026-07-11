"""replay_gateway_dlq — replays pending gateway DLQ items, idempotently.

Per doc §3.7. Reads ``pending`` records for the ``gateway`` service via
``dlq.read_pending``; for each item, checks the idempotency store for a
``replay:{event_id}`` key (skip if already replayed); resolves the target's
base URL from ``settings.gateway_urls``; attempts a signed_post_json to
``{base_url}/work``; records the outcome via ``dlq.mark_replayed`` and
flips the idempotency key on success.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import dlq, http_client
from .idempotency import GLOBAL_IDEMPOTENCY_STORE
from .settings import get_settings

_LOG = logging.getLogger("samus.replay")


def _replay_key(event_id: str) -> str:
    return f"replay:{event_id}"


async def _replay_one(item: dict[str, Any]) -> dict[str, Any]:
    event_id = item.get("event_id", "")
    target = item.get("target", "")
    if not event_id or not target:
        return {"event_id": event_id, "status": "skipped", "reason": "missing event_id or target"}

    if GLOBAL_IDEMPOTENCY_STORE.exists(_replay_key(event_id)):
        return {"event_id": event_id, "status": "skipped", "reason": "already replayed"}

    base_url = get_settings().gateway_urls.get(target)
    if not base_url:
        dlq.mark_replayed("gateway", event_id, replay_status="skipped")
        return {"event_id": event_id, "status": "skipped", "reason": "unknown target"}

    payload = item.get("payload") or {}
    try:
        resp = await http_client.signed_post_json(base_url, "/work", payload, retries=2)
        if resp.status_code < 400:
            GLOBAL_IDEMPOTENCY_STORE.set(_replay_key(event_id), True)
            dlq.mark_replayed("gateway", event_id, replay_status="replayed")
            return {"event_id": event_id, "status": "replayed"}
        dlq.mark_replayed("gateway", event_id, replay_status="replay_failed")
        return {
            "event_id": event_id,
            "status": "replay_failed",
            "reason": f"upstream {resp.status_code}",
        }
    except Exception as exc:
        dlq.mark_replayed("gateway", event_id, replay_status="replay_failed")
        return {"event_id": event_id, "status": "replay_failed", "reason": str(exc)}


async def replay_gateway_dlq(limit: int = 25) -> list[dict[str, Any]]:
    """Replay up to ``limit`` pending gateway DLQ items."""
    pending = dlq.read_pending("gateway", limit=limit)
    out: list[dict[str, Any]] = []
    for item in pending:
        result = await _replay_one(item)
        out.append(result)
        _LOG.info(
            "replay_outcome",
            extra={"event_id": result.get("event_id"), "status": result.get("status")},
        )
    return out


def replay_gateway_dlq_sync(limit: int = 25) -> list[dict[str, Any]]:
    """Synchronous convenience wrapper for CLIs / cron jobs."""
    return asyncio.run(replay_gateway_dlq(limit=limit))
