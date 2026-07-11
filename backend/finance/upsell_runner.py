"""Upsell sequence runner — drains the queue + sends emails.

Called both by the finance workcell's lifespan daemon thread (hourly) and
manually via ``python -m backend.finance.upsell_runner``.

Loop body:
  1. Read every ``queued`` row whose ``due_at <= now`` (see ``due_upsells``)
  2. For each, render the per-touch template, send via the common email
     backend, then append a ``sent`` or ``failed`` transition row
  3. Return counts: total due, sent, failed

The transition row is written EVEN IF the email send raised — that way
the next loop iteration won't re-attempt a row that already failed (the
fold-the-log read sees the failed row as the current state, which is not
``queued``, which excludes it from ``due_upsells``).

Tests inject ``send_email_fn`` to avoid hitting SendGrid. Production uses
``backend.common.email_backend.send_email``.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .upsell_queue import (
    due_upsells,
    mark_failed,
    mark_sent,
)
from .upsell_template import render_upsell_email


_LOG = logging.getLogger("samus.finance.upsell_runner")


@dataclass
class ProcessResult:
    due: int
    sent: int
    failed: int
    skipped: int


def _real_send_email(
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> dict[str, Any]:
    """Lazy default for the email backend so importing this module doesn't
    drag in the SendGrid adapter when tests inject a stub instead."""
    from backend.common.email_backend import send_email

    return send_email(to=to, subject=subject, body=body, html_body=html_body)


def process_upsell_queue(
    *,
    now: datetime | None = None,
    send_email_fn: Callable[..., dict[str, Any]] | None = None,
) -> ProcessResult:
    """Process all due rows once. Returns counts.

    Never raises — backend exceptions are translated into ``failed`` rows
    so the daemon-thread caller (which never inspects the return value)
    survives a transient SendGrid outage.
    """
    send_email_fn = send_email_fn or _real_send_email
    rows = due_upsells(now=now)
    if not rows:
        return ProcessResult(due=0, sent=0, failed=0, skipped=0)

    sent = 0
    failed = 0
    skipped = 0

    for row in rows:
        rendered = render_upsell_email(
            source_offer_code=row.source_offer_code,
            target_offer_code=row.target_offer_code,
            touch_num=row.touch_num,
            # Cut 3: thread the queue row's event_id into the payment-link
            # URL as client_reference_id=upsell_<id>. Stripe propagates it
            # to checkout.session.completed where the subscription handler
            # parses it and calls mark_converted(...) to close the loop.
            queue_event_id=row.event_id,
            # Credit application: when enqueue created a Stripe coupon, the
            # row carries its promotion_code. Threading it here causes the
            # buy link to include ?prefilled_promotion_code=<code> so the
            # discount auto-applies at the customer's checkout. Empty when
            # coupon creation failed at enqueue (best-effort — the email
            # text still mentions the credit; operator can attach manually).
            promotion_code=row.promotion_code or None,
        )
        if rendered is None:
            mark_failed(queued_row=row, error="no_composer")
            failed += 1
            continue
        subject, text_body, html_body, _payment_link = rendered

        try:
            result = send_email_fn(
                to=row.customer_email,
                subject=subject,
                body=text_body,
                html_body=html_body,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort dispatch
            _LOG.warning(
                "upsell send failed for %s touch %d: %s",
                row.customer_email,
                row.touch_num,
                exc,
            )
            mark_failed(queued_row=row, error=f"{type(exc).__name__}: {exc}")
            failed += 1
            continue

        message_id = ""
        if isinstance(result, dict):
            message_id = str(result.get("message_id") or "")
        mark_sent(queued_row=row, message_id=message_id)
        sent += 1

    return ProcessResult(due=len(rows), sent=sent, failed=failed, skipped=skipped)


# ---------------------------------------------------------------------------
# CLI: python -m backend.finance.upsell_runner [--dry-run]
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Operator-facing CLI. Default = one real pass. ``--dry-run`` = preview only."""
    parser = argparse.ArgumentParser(
        description="Drain the upsell queue (audit-to-monthly nurture sequence).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show due rows but do not send emails or write transition rows",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Override 'now' for due-window calc (ISO UTC, e.g. 2026-05-20T10:00:00Z). "
        "Useful for smoke testing — set a date AFTER your most recent enqueue's "
        "due_at to force-fire it.",
    )
    args = parser.parse_args(argv)

    override_now: datetime | None = None
    if args.now:
        try:
            override_now = datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            print(
                f"ERROR: --now must be ISO UTC like 2026-05-20T10:00:00Z; got {args.now!r}",
                flush=True,
            )
            return 2

    if args.dry_run:
        rows = due_upsells(now=override_now)
        print(f"==> Dry run: {len(rows)} due upsell row(s)", flush=True)
        for r in rows:
            print(
                f"   touch {r.touch_num}  {r.customer_email[:40]:40s}  "
                f"due={r.due_at}  source={r.source_offer_code} -> {r.target_offer_code}",
                flush=True,
            )
        return 0

    result = process_upsell_queue(now=override_now)
    print(
        f"==> Processed: due={result.due}  sent={result.sent}  failed={result.failed}",
        flush=True,
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())


__all__ = ["ProcessResult", "process_upsell_queue", "main"]
