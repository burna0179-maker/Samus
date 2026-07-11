"""Hand-ingest one inbound email through the poller's per-message pipeline.

Bridges the gap while ``SENDGRID_REPLY_TO`` still routes replies to the
operator's own inbox instead of ``samushustleforge@gmail.com`` (see
``feedback: SENDGRID_REPLY_TO routing decision``). When a client reply
lands in the operator's inbox, this script forwards its content through
the *same* ``handle_parsed_email`` call the Gmail poller uses on every
message — so classification, artifact creation, operator-task creation,
and the ``client.correspondence`` business event all fire identically to
the automated path.

Save->parse->show->approve->run:
  1. Save the email fields to a JSON file (see EXAMPLE_JSON below).
  2. Parse: this script loads + validates.
  3. Show: prints classification + owner + would-be action summary,
     then STOPS.
  4. Approve: rerun with --run to actually persist artifact + task.
  5. Run: fires ``handle_parsed_email``; prints artifact_id + task_id.

Usage inside the ``samus-intake`` container (where CRM + business_events
are wired natively)::

    docker cp payload.json samus-intake:/opt/samus/data/_ingest.json
    docker exec -w /opt/samus -e PYTHONPATH=/opt/samus samus-intake \\
        python3 scripts/Ingest-InboundEmail.py /opt/samus/data/_ingest.json
    # confirm the preview, then:
    docker exec -w /opt/samus -e PYTHONPATH=/opt/samus samus-intake \\
        python3 scripts/Ingest-InboundEmail.py /opt/samus/data/_ingest.json --run

EXAMPLE_JSON::

    {
      "message_id": "<forwarded-kerry-2026-07-10@ops.hustleforge.tech>",
      "from_addr": "<client-email>@example.com",
      "from_display": "Kerry Brown <<client-email>@example.com>",
      "to_addrs": ["ahartman@hustleforge.tech"],
      "subject": "Re: Your Hustleforge service agreement",
      "date_header": "Thu, 10 Jul 2026 14:22:00 -0400",
      "body_text": "Alex - one question about the deposit timing before I sign..."
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _make_parsed(payload: dict):
    from backend.intake.gmail_poller import ParsedInboundEmail

    required = ("from_addr", "subject", "body_text")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise SystemExit(f"payload missing required fields: {missing}")

    return ParsedInboundEmail(
        message_id=str(payload.get("message_id") or f"<synth-{payload['from_addr']}>"),
        from_addr=str(payload["from_addr"]).strip(),
        from_display=str(payload.get("from_display") or payload["from_addr"]),
        to_addrs=list(payload.get("to_addrs") or []),
        subject=str(payload["subject"]),
        date_header=str(payload.get("date_header") or ""),
        body_text=str(payload["body_text"]),
        body_format=str(payload.get("body_format") or "text"),
        attachment_names=list(payload.get("attachment_names") or []),
    )


def _preview(parsed) -> dict:
    """Show what WOULD happen without persisting anything."""
    from backend.crm.client_directory import lookup_client
    from backend.intake.email_classifier import classify

    classification = classify(parsed)
    known = lookup_client(parsed.from_addr)

    preview = {
        "from_addr": parsed.from_addr,
        "subject": parsed.subject,
        "body_len": len(parsed.body_text),
        "classification": classification.to_dict(),
        "known_client": (
            {
                "client_id": known.client_id,
                "campaign_id": known.campaign_id,
                "role": known.role,
                "display_name": known.display_name,
            }
            if known
            else None
        ),
        "would_create_artifact_kind": (
            "client_correspondence"
            if classification.category == "client_correspondence"
            else "inbound_email"
        ),
        "would_emit_business_event": (
            "client.correspondence"
            if classification.category == "client_correspondence"
            else None
        ),
    }
    return preview


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Hand-ingest one inbound email through the poller pipeline.",
    )
    ap.add_argument("payload_json", help="Path to the JSON payload.")
    ap.add_argument(
        "--run",
        action="store_true",
        help=(
            "Actually run handle_parsed_email (persist artifact + task, "
            "emit business event). WITHOUT --run this is a dry preview only."
        ),
    )
    args = ap.parse_args()

    p = Path(args.payload_json)
    if not p.exists():
        raise SystemExit(f"payload file not found: {p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {p}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"top-level JSON must be an object; got {type(payload).__name__}")

    parsed = _make_parsed(payload)

    preview = _preview(parsed)
    print("=" * 72)
    print("  INBOUND EMAIL — PREVIEW (nothing persisted yet)")
    print("=" * 72)
    print(json.dumps(preview, indent=2, ensure_ascii=False))
    print()

    if not args.run:
        print("dry-run only — rerun with --run to persist artifact + task.")
        return 0

    from backend.intake.gmail_poller import handle_parsed_email

    result = handle_parsed_email(parsed)
    print("=" * 72)
    print("  RESULT")
    print("=" * 72)
    print(
        json.dumps(
            {
                "message_id": result.message_id,
                "artifact_id": result.artifact_id,
                "operator_task_id": result.operator_task_id,
                "opportunity_id": result.opportunity_id,
                "billing_state": result.billing_state,
                "persisted": result.persisted,
                "error": result.error,
            },
            indent=2,
        )
    )
    return 0 if result.persisted else 1


if __name__ == "__main__":
    sys.exit(main())
