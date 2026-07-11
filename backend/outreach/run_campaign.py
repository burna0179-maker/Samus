"""CLI — run one Apollo-sourced email outreach campaign.

Usage:
    python -m backend.outreach.run_campaign \\
        --titles "owner,founder,marketing manager" \\
        --industries "real estate,dentist,hvac" \\
        --locations "Yuba City, California, US" \\
        --max-send 25

Flow: Apollo People Search -> unlock locked emails (People Match, capped) ->
campaign.build_messages (suppress + compliance + cap) -> for each message,
common.email_backend.send_email (SendGrid by default, in-process) + the CRM
follow-up dispatch. Appends every send to a JSONL campaign ledger and an
emailed-suppression file under the Samus artifact root, so the next run never
re-emails the same address.

Mirrors backend.prospecting.run_daily: runs in the host venv, no Docker / SQS
dependency. The host wrapper Run-OutreachDaily.ps1 seeds APOLLO_API_KEY +
SAMUS_OUTREACH_* from DPAPI / env and pins SAMUS_ARTIFACT_ROOT.

Compliance + budget:
  * Every body carries a CAN-SPAM footer (postal address + unsubscribe);
    build refuses without SAMUS_OUTREACH_POSTAL_ADDRESS + ..._UNSUBSCRIBE_URL.
  * --use-llm opts into budget-gated personalisation (default templated, $0).
  * --max-send caps a run to protect SES sender reputation during warmup.
  * --dry-run resolves + composes everything but sends nothing and writes no
    ledger — safe to run repeatedly while tuning targeting.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from backend.common.dates import iso_now

from . import apollo_source, campaign
from .campaign import _load_stake_sentence_from_opportunity
from .models import OutreachMessageRequest

_LOG = logging.getLogger("samus.outreach.run_campaign")

_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data"


def _split_csv_arg(raw: str) -> list[str]:
    return [piece.strip() for piece in (raw or "").split(",") if piece.strip()]


def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _ledger_paths() -> tuple[str, str]:
    root = os.path.join(_artifact_root(), "outreach")
    os.makedirs(root, exist_ok=True)
    today = iso_now()[:10]
    return (
        os.path.join(root, f"campaign_{today}.jsonl"),
        os.path.join(root, "emailed_emails.txt"),
    )


def _skipped_no_stake_path() -> str:
    root = os.path.join(_artifact_root(), "host_artifacts")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "outreach_skipped_no_stake.jsonl")


def _diverted_no_warmth_path() -> str:
    root = os.path.join(_artifact_root(), "host_artifacts")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "outreach_diverted_no_warmth.jsonl")


def _log_diverted_no_warmth(records: list[dict]) -> None:
    if not records:
        return
    import json as _json

    path = _diverted_no_warmth_path()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        _LOG.warning("diverted-no-warmth ledger write failed (%s): %s", path, exc)


def _apply_warmth_gate(
    contacts: list["apollo_source.ApolloContact"],
) -> tuple[list["apollo_source.ApolloContact"], list[dict]]:
    """G8 pre-flight — assess warmth for each contact; divert cold-cold.

    Returns (kept_contacts, diverted_records). Each kept contact has its
    ``legitimacy_signal`` field populated with the highest-confidence
    signal kind so downstream compose_body / Codex VW-G8 read it directly.
    Diverted contacts are persisted into the needs_warm_path queue
    (fail-CLOSED) and emitted as JSONL ledger records.
    """
    from backend.crm.needs_warm_path import (
        NeedsWarmPathPersistError,
        divert,
    )
    from backend.prospecting.legitimacy_check import (
        assess_warmth,
        highest_confidence_kind,
    )

    kept: list[apollo_source.ApolloContact] = []
    diverted: list[dict] = []
    ts = iso_now()
    for contact in contacts:
        assessment = assess_warmth(contact)
        if assessment.has_warmth:
            contact.legitimacy_signal = highest_confidence_kind(assessment.signals)
            kept.append(contact)
            continue

        prospect_id = contact.person_id or (contact.email or "").strip().lower() or "apollo_unknown"
        try:
            divert(prospect_id, prospect=contact, reason="no_legitimacy_signal")
        except NeedsWarmPathPersistError as exc:
            _LOG.error(
                "G8 divert FAIL-CLOSED for %s: %s (dropping contact, not sending)",
                prospect_id,
                exc,
            )
        diverted.append(
            {
                "ts": ts,
                "prospect_id": prospect_id,
                "email": contact.email,
                "company": contact.company,
                "reason": "no_legitimacy_signal",
            }
        )
    return kept, diverted


def _log_skipped_no_stake(records: list[dict]) -> None:
    if not records:
        return
    import json as _json

    path = _skipped_no_stake_path()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        _LOG.warning("skipped-no-stake ledger write failed (%s): %s", path, exc)


def _load_stake_sentences_for_contacts(
    contacts: list[apollo_source.ApolloContact],
    *,
    opportunity_map: dict[str, str] | None,
    fallback: dict[str, str] | None,
) -> tuple[dict[str, str], list[dict]]:
    """Resolve per-email stake_sentence + emit skipped records for misses.

    Resolution order, per contact:
      1. ``opportunity_map[email]`` -> load Opportunity + read its stake_sentence.
      2. ``fallback[email]`` -> direct mapping (operator-supplied per-run JSON).

    Contacts with neither are NOT added to the resulting map; their skip is
    appended to the returned ``skipped`` list so the caller can persist them
    to the host artifact ledger before build_messages filters them out.
    """
    opportunity_map = {k.lower(): v for k, v in (opportunity_map or {}).items()}
    fallback = {k.lower(): v for k, v in (fallback or {}).items()}
    resolved: dict[str, str] = {}
    skipped: list[dict] = []
    ts = iso_now()
    for contact in contacts:
        email = (contact.email or "").strip().lower()
        if not email:
            continue
        stake: str | None = None
        opp_id = opportunity_map.get(email, "").strip()
        if opp_id:
            stake = _load_stake_sentence_from_opportunity(opp_id)
        if not stake:
            stake = (fallback.get(email) or "").strip() or None
        if stake:
            resolved[email] = stake
        else:
            skipped.append(
                {
                    "ts": ts,
                    "email": email,
                    "company": contact.company,
                    "opportunity_id": opp_id,
                    "reason": "no_stake_sentence",
                }
            )
    return resolved, skipped


def _config_from_env(args: argparse.Namespace) -> campaign.CampaignConfig:
    return campaign.CampaignConfig(
        template_id=args.template_id,
        subject=args.subject,
        from_name=os.getenv("SAMUS_OUTREACH_FROM_NAME", "Samus"),
        sender_postal_address=os.getenv("SAMUS_OUTREACH_POSTAL_ADDRESS", ""),
        unsubscribe_url=os.getenv("SAMUS_OUTREACH_UNSUBSCRIBE_URL", ""),
        max_send=args.max_send,
        require_verified_email=not args.allow_unverified,
        use_llm=args.use_llm,
    )


def _gather_contacts(args: argparse.Namespace) -> list[apollo_source.ApolloContact]:
    titles = _split_csv_arg(args.titles)
    industries = _split_csv_arg(args.industries)
    locations = _split_csv_arg(args.locations)

    pool: list[apollo_source.ApolloContact] = []
    for page in range(1, max(1, args.pages) + 1):
        batch = apollo_source.search_people(
            titles=titles,
            industries=industries,
            locations=locations,
            per_page=args.per_page,
            page=page,
        )
        if not batch:
            break
        pool.extend(batch)

    # Unlock locked emails for the most promising contacts, capped at the
    # send limit so we never burn Apollo credits beyond what we can email.
    unlock_budget = args.max_send
    enriched: list[apollo_source.ApolloContact] = []
    for contact in pool:
        if contact.email_locked and unlock_budget > 0:
            contact = apollo_source.enrich_person(contact)
            unlock_budget -= 1
        enriched.append(contact)
    return enriched


def _send_all(
    messages: list[OutreachMessageRequest],
    ledger_path: str,
    suppression_path: str,
) -> tuple[int, int]:
    """Send each message via the configured email backend; append successes to
    the ledger + suppression file. Returns (sent, failed).

    Sends through ``common.email_backend.send_email`` — the provider selector
    that honours ``settings.email_backend`` (SendGrid by default; SES once it
    is re-approved + lifted into the selector). This is deliberately NOT the
    outreach workcell's ``send_message``, which is still hardcoded to SES.

    The CRM follow-up loop is preserved by reusing the workcell's own
    ``_dispatch_outreach_to_crm`` after each successful send, so an emailed
    prospect still surfaces as a follow-up call 2 days later (best-effort).
    """
    import json as _json

    from backend.common.email_backend import send_email

    from .service import _dispatch_outreach_to_crm

    sent = failed = 0
    with (
        open(ledger_path, "a", encoding="utf-8") as ledger,
        open(suppression_path, "a", encoding="utf-8") as supp,
    ):
        from .flyer import flyer_html_for

        for req in messages:
            try:
                # Attach the vibrant buy-now flyer so cold-campaign emails
                # convert on impulse instead of reading like plain text. Use an
                # already-set html_body if the builder provided one; else render
                # from the message's own fields. Fail-soft -> text-only on "".
                _flyer = (req.html_body or "") or flyer_html_for(
                    prospect_id=req.prospect_id,
                    to_email=req.to,
                    row={"company_name": getattr(req, "company", "") or ""},
                    stake=(req.body or "")[:280],
                )
                result = send_email(
                    to=req.to,
                    subject=req.subject,
                    body=req.body,
                    html_body=(_flyer or None),
                    from_name=os.getenv("SAMUS_OUTREACH_FROM_NAME") or None,
                )
                sent += 1
                ts = str(result.get("ts") or iso_now())
                ledger.write(
                    _json.dumps(
                        {
                            "ts": ts,
                            "prospect_id": req.prospect_id,
                            "email": req.to,
                            "company": req.company,
                            "template_id": req.template_id,
                            "message_id": result.get("message_id", ""),
                            "backend": "email_backend",
                            "status": "sent",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                supp.write((req.to or "") + "\n")
                try:
                    _dispatch_outreach_to_crm(req, ts)
                except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail a send
                    _LOG.warning(
                        "crm follow-up dispatch failed prospect=%s: %s", req.prospect_id, exc
                    )
            except Exception as exc:  # noqa: BLE001 — one bad send must not abort the run
                failed += 1
                _LOG.warning("send failed prospect=%s to=%s: %s", req.prospect_id, req.to, exc)
    return sent, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.outreach.run_campaign",
        description="Run one Apollo-sourced email outreach campaign.",
    )
    parser.add_argument(
        "--titles", default="", help="Comma-separated person titles, e.g. 'owner,founder'"
    )
    parser.add_argument("--industries", default="", help="Comma-separated industry keywords")
    parser.add_argument(
        "--locations",
        default="",
        help="Comma-separated person locations, e.g. 'Yuba City, California, US'",
    )
    parser.add_argument("--per-page", type=int, default=25)
    parser.add_argument(
        "--pages", type=int, default=1, help="How many search pages to pull (per_page each)"
    )
    parser.add_argument(
        "--max-send", type=int, default=25, help="Hard cap on emails sent this run (SES warmup)"
    )
    parser.add_argument("--template-id", default="apollo_cold_v1")
    parser.add_argument("--subject", default="Quick question about {company}")
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Budget-gated LLM personalisation (default: templated)",
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="Also email guessed/unverified addresses (default: verified only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve + compose but send nothing and write no ledger",
    )
    parser.add_argument(
        "--stake-sentences-json",
        default="",
        help="Path to a JSON object mapping {email: stake_sentence} for cold contacts",
    )
    parser.add_argument(
        "--opportunity-map-json",
        default="",
        help="Path to a JSON object mapping {email: opportunity_id} to source stakes from CRM",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Load the Codex Validation Layer before any Apollo/compose call. The Apollo
    # G11 preflight and the compose-body gate both refuse fail-closed when the
    # registry isn't loaded. HTTP workcells get this via app_factory; this host
    # CLI must do it explicitly (mirrors backend.cash_engine.worker.main).
    from backend.common.app_factory import _ensure_codex_loaded

    _ensure_codex_loaded("outreach")

    cfg = _config_from_env(args)
    ledger_path, suppression_path = _ledger_paths()

    try:
        contacts = _gather_contacts(args)
    except apollo_source.ApolloError as exc:
        _LOG.error("apollo source failed: %s", exc)
        return 2

    already_sent = campaign.load_suppression(suppression_path)

    opportunity_map: dict[str, str] = {}
    fallback_stakes: dict[str, str] = {}
    if args.opportunity_map_json:
        try:
            import json as _json

            with open(args.opportunity_map_json, encoding="utf-8") as fh:
                opportunity_map = {str(k): str(v) for k, v in (_json.load(fh) or {}).items()}
        except (OSError, ValueError) as exc:
            _LOG.error("opportunity-map-json read failed: %s", exc)
            return 2
    if args.stake_sentences_json:
        try:
            import json as _json

            with open(args.stake_sentences_json, encoding="utf-8") as fh:
                fallback_stakes = {str(k): str(v) for k, v in (_json.load(fh) or {}).items()}
        except (OSError, ValueError) as exc:
            _LOG.error("stake-sentences-json read failed: %s", exc)
            return 2

    # G8 — pre-flight legitimacy filter. Cold-cold contacts are diverted to
    # the needs_warm_path queue BEFORE stake resolution / compose / send.
    contacts, diverted = _apply_warmth_gate(contacts)
    if diverted and not args.dry_run:
        _log_diverted_no_warmth(diverted)
    if diverted:
        _LOG.warning(
            "outreach diverted %d contact(s) with no legitimacy signal (G8)",
            len(diverted),
        )

    stake_lookup, skipped = _load_stake_sentences_for_contacts(
        contacts,
        opportunity_map=opportunity_map,
        fallback=fallback_stakes,
    )
    if skipped and not args.dry_run:
        _log_skipped_no_stake(skipped)
    if skipped:
        _LOG.warning(
            "outreach skipping %d contact(s) with no stake_sentence on file",
            len(skipped),
        )

    try:
        result = campaign.build_messages(
            contacts,
            cfg,
            already_sent=already_sent,
            stake_sentences=stake_lookup,
        )
    except ValueError as exc:
        _LOG.error("campaign build refused: %s", exc)
        return 2

    print(
        f"considered={result.considered} built={result.built} "
        f"suppressed={result.suppressed} not_sendable={result.not_sendable} "
        f"duplicate={result.duplicate} capped={result.capped}"
    )

    if args.dry_run:
        for req in result.messages[:5]:
            print(f"  DRY -> {req.to}  subj={req.subject!r}")
        print(f"DRY RUN -- would send {result.built} email(s); no ledger written.")
        return 0

    sent, failed = _send_all(result.messages, ledger_path, suppression_path)
    print(f"sent={sent} failed={failed}")
    print(f"  ledger -> {ledger_path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
