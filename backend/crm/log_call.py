"""Operator call-logging — record a hand-dialed call into the CRM.

The morning call list (``morning_call_list_<date>.txt``) is worked by hand;
this is how the operator inputs each call's outcome + conversation notes. It
writes a :class:`Conversation` (notes + outcome) and refreshes the prospect's
:class:`CallState` through the canonical CRM service layer — the same path the
automated Vapi webhook uses — so manually-dialed calls land in the same tables
the morning brief reads ("booked from yesterday's calls").

Resilience: every logged call is also appended to an operator journal,
``call_outcomes_<date>.jsonl`` under ``<artifact_root>/daily_calls/``. The
journal is written first, so a call is never lost even if the CRM/DynamoDB
write fails — the journal line can be re-ingested later.

Invoked per-call by ``scripts/Log-Call.ps1`` (which loads AWS credentials from
DPAPI). Non-interactive: one call logged per invocation.

  python -m backend.crm.log_call --prospect-id pr_x --company "Acme HVAC" \\
      --outcome booked --notes "spoke with owner, booked audit Thu 2pm"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from backend.common import storage
from backend.common.dates import iso_now
from backend.crm import service as crm_service

# Canonical outcome taxonomy — shared with the Vapi agent path
# (backend.voice.service) so both record into the same vocabulary.
from backend.crm.call_outcomes import (
    VALID_OUTCOMES,
    is_valid_outcome,
    state_for_outcome,
)
from backend.crm.models import CallState, Conversation, CreateOpportunityRequest

_LOG = logging.getLogger("samus.crm.log_call")


def _journal_path(today: date | None = None) -> Path:
    """Operator journal — co-located with the call list under daily_calls/."""
    day = today or date.today()
    return storage.root() / "daily_calls" / f"call_outcomes_{day.isoformat()}.jsonl"


def _append_journal(record: dict[str, Any]) -> bool:
    """Append one call record to the operator journal. Never raises."""
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return True
    except OSError as exc:  # noqa: BLE001 — journal failure must not block logging
        _LOG.warning("call-outcome journal append failed: %s", exc)
        return False


def log_call(
    *,
    prospect_id: str,
    company: str = "",
    outcome: str,
    notes: str = "",
    phone: str = "",
    duration_sec: int = 0,
    industry: str = "",
    policy_family: str = "",
    seo_score: int = 0,
    owner_email: bool = False,
    social_facebook: bool = False,
    social_instagram: bool = False,
    token_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Record one hand-dialed call. Returns a result dict; never raises.

    Writes the operator journal first (so the call is never lost), then a
    ``Conversation`` + ``CallState`` via the CRM service layer.

    ``industry`` / ``policy_family`` / ``seo_score`` / ``owner_email`` /
    ``social_facebook`` / ``social_instagram`` are the strategy-bandit
    attribution snapshot (Unit 3) — threaded from the call-list CSV row by
    ``Log-Call.ps1``. They are forwarded into the ``CreateOpportunityRequest``
    a ``booked`` call mints, so the eventual deal close can be credited back to
    the bandit arm that picked this prospect. All default empty for a manual
    call with no call-list provenance.

    ``token_cost_usd`` (strategy-integration build, Unit 4) is the per-prospect
    LLM dollars spent during discovery, also threaded from the call-list CSV;
    it rides into the same ``CreateOpportunityRequest`` so the eventual deal's
    reward signal can weigh a cheap win against an expensive one.
    """
    outcome = (outcome or "").strip().lower()
    if not is_valid_outcome(outcome):
        return {
            "ok": False,
            "error": f"invalid_outcome: {outcome!r} (expected one of {', '.join(VALID_OUTCOMES)})",
        }
    if not (prospect_id or "").strip():
        return {"ok": False, "error": "prospect_id is required"}

    ts = iso_now()
    record = {
        "ts": ts,
        "prospect_id": prospect_id,
        "company": company,
        "phone": phone,
        "outcome": outcome,
        "notes": notes,
        "duration_sec": duration_sec,
        "source": "operator",
    }
    journal_ok = _append_journal(record)

    conv = Conversation(
        conversation_id=crm_service._new_conversation_id(),
        prospect_id=prospect_id,
        channel="call",
        status="completed",
        started_at=ts,
        ended_at=ts,
        duration_sec=duration_sec,
        transcript=notes,
        outcome=outcome,
        source="operator",
        source_ref="manual_call",
    )
    prior = None
    try:
        prior = crm_service.get_call_state(prospect_id)
    except Exception as exc:  # noqa: BLE001 — degraded read; attempt_count restarts at 1
        _LOG.warning("get_call_state(%s) failed: %s", prospect_id, exc)
    state = CallState(
        prospect_id=prospect_id,
        state=state_for_outcome(outcome),
        attempt_count=(prior.attempt_count if prior else 0) + 1,
        last_attempt_at=ts,
        last_outcome=outcome,
        notes=notes,
        updated_at=ts,
    )

    try:
        conv_ok = crm_service.upsert_conversation(conv)
    except Exception as exc:  # noqa: BLE001 — journal already has the call
        _LOG.warning("upsert_conversation failed: %s", exc)
        conv_ok = False
    try:
        state_ok = crm_service.upsert_call_state(state)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("upsert_call_state failed: %s", exc)
        state_ok = False

    # A booked call is a real deal — open a tracked Opportunity so it enters
    # the pipeline (stage=new) instead of living only as a conversation row.
    # Best-effort: a degraded opportunity write does not fail the call log.
    opportunity_id = ""
    if outcome == "booked":
        try:
            opp = crm_service.create_opportunity(
                CreateOpportunityRequest(
                    prospect_id=prospect_id,
                    name=company or f"Opportunity for {prospect_id}",
                    intent_score=85,  # a booked call is high-intent
                    next_step=notes or "Follow up on booked call",
                    # Strategy bandit attribution snapshot (Unit 3) — carried from
                    # the call-list CSV so a closed deal credits the right arm.
                    industry=industry,
                    policy_family=policy_family,
                    seo_score=seo_score,
                    owner_email=owner_email,
                    social_facebook=social_facebook,
                    social_instagram=social_instagram,
                    # Per-prospect LLM cost (Unit 4) — carried from the call-list CSV.
                    token_cost_usd=token_cost_usd,
                )
            )
            if opp.status == "created":
                opportunity_id = opp.opportunity_id
            else:
                _LOG.warning("create_opportunity degraded: %s", opp.error)
        except Exception as exc:  # noqa: BLE001 — call log already persisted
            _LOG.warning("create_opportunity failed: %s", exc)

    # Distill structured intelligence from operator notes. Best-effort:
    # the call is already persisted; a distiller fault loses enrichment,
    # not the call itself.
    distilled: dict[str, Any] = {}
    if notes:
        try:
            from backend.crm.note_distiller import distill_notes

            distilled = distill_notes(
                prospect_id=prospect_id,
                company=company,
                notes=notes,
                outcome=outcome,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("note_distiller failed for %s: %s", prospect_id, exc)

    return {
        "ok": bool(conv_ok and state_ok),
        "prospect_id": prospect_id,
        "outcome": outcome,
        "conversation_id": conv.conversation_id,
        "conversation_persisted": bool(conv_ok),
        "call_state_persisted": bool(state_ok),
        "opportunity_id": opportunity_id,
        "journal_persisted": journal_ok,
        "journal_path": str(_journal_path()),
        "distilled": distilled,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry — log one call. Exit 0 on a clean CRM write, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="log_call",
        description="Record one hand-dialed call into the CRM.",
    )
    parser.add_argument("--prospect-id", required=True)
    parser.add_argument("--company", default="")
    parser.add_argument("--phone", default="")
    parser.add_argument("--outcome", required=True, choices=VALID_OUTCOMES)
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--duration", type=int, default=0, help="call duration in seconds (optional)"
    )
    # --- strategy bandit attribution snapshot (Unit 3) --------------------
    # Threaded from the call-list CSV row by Log-Call.ps1. seo_score is the
    # 0-100 audit score; the three social/owner flags are booleans ("did
    # enrichment find one"), passed as --owner-email-found etc.
    parser.add_argument("--industry", default="")
    parser.add_argument("--policy-family", default="")
    parser.add_argument(
        "--seo-score", type=int, default=0, help="0-100 SEO audit score from the call-list row"
    )
    parser.add_argument(
        "--owner-email-found",
        action="store_true",
        help="enrichment found an owner email for this prospect",
    )
    parser.add_argument(
        "--social-facebook-found",
        action="store_true",
        help="enrichment found a Facebook handle for this prospect",
    )
    parser.add_argument(
        "--social-instagram-found",
        action="store_true",
        help="enrichment found an Instagram handle for this prospect",
    )
    # --- per-prospect LLM cost (strategy-integration build, Unit 4) -------
    # Threaded from the call-list CSV row by Log-Call.ps1.
    parser.add_argument(
        "--token-cost-usd",
        type=float,
        default=0.0,
        help="per-prospect LLM dollars spent during discovery",
    )
    args = parser.parse_args(argv)

    result = log_call(
        prospect_id=args.prospect_id,
        company=args.company,
        phone=args.phone,
        outcome=args.outcome,
        notes=args.notes,
        duration_sec=args.duration,
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
    # CRM write degraded — the journal still captured it; signal non-zero so
    # the operator wrapper can flag it, but the call data is not lost.
    return 1


if __name__ == "__main__":
    sys.exit(main())
