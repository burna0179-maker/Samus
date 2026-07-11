"""Gateway routes for the experiment registry (Tranche 3).

Registered onto the gateway app via one additive line in
``backend/gateway/app.py`` (``register_routes(app)``), keeping the app module
edit minimal per the tranche's file-ownership rules.

  * ``GET  /admin/experiments``  — registry listing + per-arm bandit stats
    (capability ``budget_admin``, matching the other read-only /admin views).
  * ``POST /admin/experiments``  — register an experiment
    (capability ``control_tick``, matching gateway mutation routes).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from backend.common.capabilities import check_capability

from . import promoter, registry

_LOG = logging.getLogger("samus.experiments.routes")


def register_routes(app: Any) -> None:
    """Attach the /admin/experiments routes to a FastAPI app."""

    @app.get("/admin/experiments")
    async def admin_experiments_list() -> dict[str, Any]:
        """Operator view: every experiment + its per-arm bandit stats."""
        check_capability("gateway", "budget_admin")
        rows: list[dict[str, Any]] = []
        for exp in registry.list_experiments():
            row = exp.to_dict()
            row["arm_stats"] = registry.arm_stats(exp.experiment_id)
            rows.append(row)
        return {
            "experiments": rows,
            "campaign_halted": promoter.campaign_halted(),
            "recent_assignments": registry.assignments_tail(limit=25),
        }

    @app.get("/admin/experiments/{experiment_id}/uplift")
    async def admin_experiment_uplift(experiment_id: str) -> dict[str, Any]:
        """Causal view: each arm's uplift vs the control (incumbent) arm, with
        significance + spurious-risk — answers "does this arm actually beat the
        baseline?", not just "which arm has the highest rate?"."""
        check_capability("gateway", "budget_admin")
        from . import uplift

        return uplift.uplift_report(experiment_id)

    @app.post("/admin/experiments")
    async def admin_experiments_register(request: Request) -> dict[str, Any]:
        """Register (or replace) an experiment.

        Body: ``{"dimension": "...", "arms": [...], "experiment_id"?,
        "min_trials"?, "metadata"?}``.
        """
        check_capability("gateway", "control_tick")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed JSON is a client error
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        try:
            exp = registry.register_experiment(
                dimension=str(body.get("dimension", "")),
                arms=[str(a) for a in (body.get("arms") or [])],
                experiment_id=str(body.get("experiment_id", "") or ""),
                min_trials=int(body.get("min_trials", registry.DEFAULT_MIN_TRIALS)),
                metadata=(body.get("metadata") if isinstance(body.get("metadata"), dict) else None),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "experiment": exp.to_dict()}


__all__ = ["register_routes"]
