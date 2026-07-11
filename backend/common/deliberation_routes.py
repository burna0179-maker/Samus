"""Gateway route for the deliberation router (value-of-computation).

Registered onto the gateway app via one additive line in
``backend/gateway/app.py`` (``register_routes(app)``), mirroring the experiments
routes. ``fastapi`` is imported at module top (as in ``experiments/routes.py``)
so FastAPI can resolve the ``Request`` annotation; this module is only imported
by the gateway's ``create_app``, and the deliberation LOGIC (``deliberation.py``)
stays fastapi-free for non-HTTP consumers.

  * ``POST /admin/deliberate`` — given a task's {value, urgency, uncertainty,
    reversibility}, return the recommended reasoning depth + rationale
    (capability ``budget_admin``, matching the other read-only /admin views).
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from backend.common import deliberation
from backend.common.capabilities import check_capability


def register_routes(app: Any) -> None:
    """Attach the /admin/deliberate route to a FastAPI app."""

    @app.post("/admin/deliberate")
    async def admin_deliberate(request: Request) -> dict[str, Any]:
        """Recommend how hard to think about a task.

        Body: ``{"value": 0..1, "urgency"?: 0..1, "uncertainty"?: 0..1,
        "reversibility"?: 0..1, "workcell"?: str, "base_tokens"?: int}``.
        """
        check_capability("gateway", "budget_admin")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed JSON is a client error
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(body, dict) or "value" not in body:
            raise HTTPException(status_code=400, detail="body must include 'value'")
        try:
            decision = deliberation.deliberate(
                value=float(body.get("value", 0.0)),
                urgency=float(body.get("urgency", 0.0)),
                uncertainty=float(body.get("uncertainty", 0.5)),
                reversibility=float(body.get("reversibility", 1.0)),
                workcell=(str(body["workcell"]) if body.get("workcell") else None),
                base_tokens=int(body.get("base_tokens", 4000)),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return decision.to_dict()


__all__ = ["register_routes"]
