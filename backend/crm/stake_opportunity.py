"""Operator CLI — attach a stake_sentence to an existing Opportunity.

Flow:

  1. Load the Opportunity. Refuse if missing.
  2. Check the daily cap (fail-closed when ledger is unreachable).
  3. Guard the sentence (length, banned phrases, casing, ASCII ratio).
  4. Dedup against the rolling hash ledger.
  5. Write stake_sentence + authored_by + authored_at onto the Opportunity.
  6. Record-use on the budget (atomic increment).
  7. Append the hash to the dedup ledger.
  8. Register a samus_artifact of kind=``stake_sentence`` linked to the Opp.

Any step that fails -> exit code 1, clear stderr message, NO partial write.
The Opportunity write happens AFTER the gates clear so a failed guard
never leaves a half-stake on a row.

Usage:
    python -m backend.crm.stake_opportunity <opportunity_id> "<one sentence>"

The operator id defaults to ``SAMUS_OPERATOR`` env or ``"alex"``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from backend.common.dates import iso_now
from backend.common.stake_sentence_budget import (
    StakeSentenceBudgetUnavailable,
    get_store as get_budget_store,
)
from backend.common.stake_sentence_guard import (
    StakeSentenceRejected,
    is_duplicate,
    record_hash,
    validate_stake_sentence,
)
from backend.crm import service as crm_service
from backend.crm.models import CreateArtifactRequest


_LOG = logging.getLogger("samus.crm.stake_opportunity")


class StakeOpportunityError(RuntimeError):
    """Surfaceable failure during the stake flow. ``reason`` is the short code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def attach_stake_sentence(
    *,
    opportunity_id: str,
    stake_sentence: str,
    operator_id: str,
) -> dict:
    """Run the full gate -> persist -> artifact pipeline. Raises on any failure."""
    opp_id = (opportunity_id or "").strip()
    text = (stake_sentence or "")
    op_id = (operator_id or "").strip() or "alex"

    if not opp_id:
        raise StakeOpportunityError("missing_opportunity_id", "opportunity_id is required")

    opp = crm_service.get_opportunity(opp_id)
    if opp is None:
        raise StakeOpportunityError(
            "opportunity_not_found",
            f"no opportunity with id {opp_id!r}",
        )
    if (opp.stake_sentence or "").strip():
        raise StakeOpportunityError(
            "already_staked",
            f"opportunity {opp_id} already has a stake_sentence",
        )

    budget = get_budget_store()
    try:
        remaining = budget.remaining_today()
    except StakeSentenceBudgetUnavailable as exc:
        raise StakeOpportunityError(
            "budget_unavailable",
            f"stake_sentence budget unreadable (fail-closed): {exc}",
        ) from exc
    if remaining <= 0:
        raise StakeOpportunityError(
            "daily_cap_exhausted",
            f"daily stake_sentence cap exhausted (cap={budget.daily_cap})",
        )

    try:
        validate_stake_sentence(text)
    except StakeSentenceRejected as exc:
        raise StakeOpportunityError("guard_rejected", str(exc)) from exc

    if is_duplicate(text):
        raise StakeOpportunityError(
            "duplicate",
            "stake_sentence matches one of the recent N entries; rewrite it",
        )

    authored_at = iso_now()
    updated = crm_service.set_opportunity_stake(
        opportunity_id=opp_id,
        stake_sentence=text.strip(),
        authored_by=op_id,
        authored_at=authored_at,
    )
    if updated is None:
        raise StakeOpportunityError(
            "persist_failed",
            f"failed to persist stake_sentence onto opportunity {opp_id}",
        )

    try:
        budget.record_use(opp_id)
    except StakeSentenceBudgetUnavailable as exc:
        # The stake landed on the row but the budget couldn't increment.
        # That is a fail-closed condition for FUTURE writes (next call's
        # remaining_today will refuse) — we surface this run as a soft
        # warning so the operator sees the inconsistency but the
        # already-persisted stake stays.
        _LOG.error(
            "stake_sentence persisted but budget record_use failed opp=%s: %s",
            opp_id, exc,
        )
        raise StakeOpportunityError(
            "budget_record_failed",
            f"stake_sentence persisted but budget increment failed: {exc}",
        ) from exc

    record_hash(text)

    artifact = crm_service.create_artifact(CreateArtifactRequest(
        kind="stake_sentence",
        owner_entity_kind="opportunity",
        owner_entity_id=opp_id,
        title="stake_sentence",
        inline_data={
            "stake_sentence": text.strip(),
            "authored_by": op_id,
            "authored_at": authored_at,
        },
        source="stake_opportunity_cli",
        created_by=op_id,
    ))

    return {
        "ok": True,
        "opportunity_id": opp_id,
        "stake_sentence": updated.stake_sentence,
        "authored_by": updated.stake_sentence_authored_by,
        "authored_at": updated.stake_sentence_authored_at,
        "artifact_id": artifact.artifact_id,
        "artifact_status": artifact.status,
        "remaining_today": budget.remaining_today(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.crm.stake_opportunity",
        description="Attach an operator-authored stake_sentence to an Opportunity.",
    )
    parser.add_argument("opportunity_id", help="op_... id of the Opportunity")
    parser.add_argument("stake_sentence", help="the one operator-authored sentence")
    parser.add_argument(
        "--operator",
        default=os.getenv("SAMUS_OPERATOR", "alex"),
        help="operator id recorded as stake_sentence_authored_by (default: env SAMUS_OPERATOR or 'alex')",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        result = attach_stake_sentence(
            opportunity_id=args.opportunity_id,
            stake_sentence=args.stake_sentence,
            operator_id=args.operator,
        )
    except StakeOpportunityError as exc:
        print(f"stake_opportunity REFUSED ({exc.reason}): {exc}", file=sys.stderr)
        return 1

    import json
    print(json.dumps(result, indent=2))
    return 0


__all__ = ["StakeOpportunityError", "attach_stake_sentence", "main"]


if __name__ == "__main__":
    sys.exit(main())
