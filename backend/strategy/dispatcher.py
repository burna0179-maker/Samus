"""Strategy action dispatcher.

Maps engine decisions to gateway POST calls via signed_post_json.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

from backend.common.http_client import signed_post_json

from .engine import StrategyContext

_LOG = logging.getLogger("samus.strategy.dispatcher")

SAMUS_GATEWAY_URL: str = os.getenv("SAMUS_GATEWAY_URL", "http://samus-gateway:8080")

# Maps engine action -> (service, gateway_action)
_ACTION_MAP: dict[str, tuple[str, str]] = {
    "replan_fulfillment": ("fulfillment", "resume_plan"),
    "trigger_outreach": ("outreach", "send_outreach"),
    "escalate_close": ("outreach", "send_close"),
}


async def dispatch_strategy_action(
    decision: dict[str, Any],
    ctx: StrategyContext,
) -> dict[str, Any]:
    """Dispatch an engine decision to the appropriate gateway target.

    For "monitor" and "none" actions no HTTP call is made and
    ``dispatched=False`` is returned.

    Returns a dict with keys: dispatched, service, action,
    gateway_status (when dispatched) or reason (when not dispatched).
    """
    action = decision.get("action", "none")

    if action not in _ACTION_MAP:
        _LOG.debug("dispatch_strategy_action no dispatch for action=%s", action)
        return {"dispatched": False, "reason": "no_action_for_decision"}

    service, gateway_action = _ACTION_MAP[action]
    task_id = f"strategy-{ctx.prospect_id}-{uuid4()}"
    payload = {"prospect_id": ctx.prospect_id}

    envelope = {
        "task_id": task_id,
        "payload": payload,
        "metadata": {"action": gateway_action},
    }

    path = f"/dispatch/{service}"
    try:
        response = await signed_post_json(SAMUS_GATEWAY_URL, path, payload=envelope)
        _LOG.info(
            "dispatch_strategy_action sent action=%s service=%s status=%s task_id=%s",
            action,
            service,
            response.status_code,
            task_id,
        )
        return {
            "dispatched": True,
            "service": service,
            "action": gateway_action,
            "gateway_status": response.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.error(
            "dispatch_strategy_action failed action=%s service=%s error=%s",
            action,
            service,
            exc,
        )
        return {
            "dispatched": False,
            "reason": f"dispatch_error: {exc}",
        }
