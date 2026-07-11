"""Operator opportunity-creation — open a tracked CRM Opportunity for a deal.

The booked->Opportunity wiring in :mod:`backend.crm.log_call` fires only when a
call is logged ``booked`` at first-dial time. A deal that materialises later —
a follow-up call that converts, or a hand-dialed prospect who agreed to a
specific product — has no path into the pipeline. This is that path: it mints
one Opportunity through the canonical CRM service layer.

The minted ``opportunity_id`` ("op_...") is the exact-attribution key: tag a
Stripe buy link with ``?client_reference_id=<opportunity_id>`` and the finance
webhook advances THIS opportunity to ``closed_won`` when the customer pays —
see :func:`backend.finance.webhook._dispatch_close_opportunity_by_id`.

Invoked by ``scripts/Create-Opportunity.ps1`` (which loads AWS credentials from
DPAPI). Non-interactive: one opportunity per invocation.

  python -m backend.crm.create_opportunity --prospect-id pr_x \\
      --name "Acme HVAC - SEO Audit" --intent-score 85 \\
      --service-interest seo_audit --next-step "sent the $149 audit buy link"
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from backend.crm import service as crm_service
from backend.crm.models import CreateOpportunityRequest

_LOG = logging.getLogger("samus.crm.create_opportunity")


def _existing_opportunities(prospect_id: str) -> list[str]:
    """Return ids of opportunities already on this prospect (any stage).

    Best-effort — a degraded read returns ``[]`` so a transient DDB hiccup
    doesn't block a deliberate operator create.
    """
    try:
        result = crm_service.list_opportunities(limit=500)
    except Exception as exc:  # noqa: BLE001 — degraded read; do not block create
        _LOG.warning("opportunity pre-check scan failed: %s", exc)
        return []
    return [
        o.opportunity_id for o in result.opportunities
        if o.prospect_id == prospect_id
    ]


def create_opportunity(
    *,
    prospect_id: str,
    name: str = "",
    intent_score: int | None = None,
    service_interest: list[str] | None = None,
    next_step: str = "",
    assigned_to: str = "",
    monthly_budget: str = "",
    force: bool = False,
    industry: str = "",
    policy_family: str = "",
    seo_score: int = 0,
    owner_email: bool = False,
    social_facebook: bool = False,
    social_instagram: bool = False,
    token_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Open one tracked Opportunity for a deal. Returns a result dict; never raises.

    When the prospect already has an Opportunity, returns ``status="exists"``
    without creating a second one — pass ``force=True`` to override (e.g. a
    repeat customer opening a genuinely separate deal).

    ``industry`` / ``policy_family`` / ``seo_score`` / ``owner_email`` /
    ``social_facebook`` / ``social_instagram`` are the strategy-bandit
    attribution snapshot (Unit 3): pass them so the eventual deal close credits
    the bandit arm that picked the prospect. The operator supplies them from the
    call-list row via ``Create-Opportunity.ps1``; all default empty.

    ``token_cost_usd`` (strategy-integration build, Unit 4) is the per-prospect
    LLM dollars spent during discovery, also taken from the call-list row, so
    the deal's reward signal can weigh a cheap win against an expensive one.
    """
    prospect_id = (prospect_id or "").strip()
    if not prospect_id:
        return {"ok": False, "status": "rejected",
                "error": "prospect_id is required"}

    existing = _existing_opportunities(prospect_id)
    if existing and not force:
        return {
            "ok": False,
            "status": "exists",
            "prospect_id": prospect_id,
            "existing_opportunity_ids": existing,
            "error": f"prospect already has {len(existing)} opportunity(ies); "
                     "pass --force to open another",
        }

    try:
        result = crm_service.create_opportunity(CreateOpportunityRequest(
            prospect_id=prospect_id,
            name=name,
            intent_score=intent_score,
            service_interest=list(service_interest or []),
            next_step=next_step,
            assigned_to=assigned_to,
            monthly_budget=monthly_budget,
            # Strategy bandit attribution snapshot (Unit 3).
            industry=industry,
            policy_family=policy_family,
            seo_score=seo_score,
            owner_email=owner_email,
            social_facebook=social_facebook,
            social_instagram=social_instagram,
            # Per-prospect LLM cost (Unit 4).
            token_cost_usd=token_cost_usd,
        ))
    except Exception as exc:  # noqa: BLE001 — surface, never raise out
        _LOG.warning("create_opportunity failed: %s", exc)
        return {"ok": False, "status": "failed", "prospect_id": prospect_id,
                "error": f"create_opportunity_raised: {exc}"}

    return {
        "ok": result.status == "created",
        "status": result.status,
        "prospect_id": prospect_id,
        "opportunity_id": result.opportunity_id,
        "existing_opportunity_ids": existing,
        "ts": result.ts,
        "error": result.error,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry — open one Opportunity. Exit 0 on a clean create, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="create_opportunity",
        description="Open one tracked CRM Opportunity for a deal.",
    )
    parser.add_argument("--prospect-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--intent-score", type=int, default=None,
                        help="0-100 intent signal; a booked deal is ~85")
    parser.add_argument("--service-interest", action="append", default=[],
                        metavar="CODE",
                        help="repeatable; e.g. --service-interest seo_audit")
    parser.add_argument("--next-step", default="")
    parser.add_argument("--assigned-to", default="", help="operator email")
    parser.add_argument("--monthly-budget", default="",
                        help="intake budget enum, e.g. $2000-$5000 (optional)")
    parser.add_argument("--force", action="store_true",
                        help="create even if the prospect already has an opportunity")
    # --- strategy bandit attribution snapshot (Unit 3) --------------------
    parser.add_argument("--industry", default="",
                        help="prospect industry — the bandit arm's vertical")
    parser.add_argument("--policy-family", default="",
                        help="policy family the strategy bandit picked for this prospect")
    parser.add_argument("--seo-score", type=int, default=0,
                        help="0-100 SEO audit score from the call-list row")
    parser.add_argument("--owner-email-found", action="store_true",
                        help="enrichment found an owner email for this prospect")
    parser.add_argument("--social-facebook-found", action="store_true",
                        help="enrichment found a Facebook handle for this prospect")
    parser.add_argument("--social-instagram-found", action="store_true",
                        help="enrichment found an Instagram handle for this prospect")
    # --- per-prospect LLM cost (strategy-integration build, Unit 4) -------
    parser.add_argument("--token-cost-usd", type=float, default=0.0,
                        help="per-prospect LLM dollars spent during discovery")
    args = parser.parse_args(argv)

    result = create_opportunity(
        prospect_id=args.prospect_id,
        name=args.name,
        intent_score=args.intent_score,
        service_interest=args.service_interest,
        next_step=args.next_step,
        assigned_to=args.assigned_to,
        monthly_budget=args.monthly_budget,
        force=args.force,
        industry=args.industry,
        policy_family=args.policy_family,
        seo_score=args.seo_score,
        owner_email=args.owner_email_found,
        social_facebook=args.social_facebook_found,
        social_instagram=args.social_instagram_found,
        token_cost_usd=args.token_cost_usd,
    )
    print(json.dumps(result, indent=2))
    if result.get("ok"):
        return 0
    # Non-zero so the operator wrapper can flag it — the prospect already has
    # an opportunity, or the DDB write was degraded.
    return 1


if __name__ == "__main__":
    sys.exit(main())
