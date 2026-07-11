"""CLI for the post-call reconciliation sweep (Gap-8 recovery mechanism).

    python -m backend.voice.reconcile_cli

Backfills end_of_call events for today's initiated dials whose Vapi
end-of-call-report webhook never arrived. Safe to run repeatedly
(idempotent) and on a schedule. One JSON line of summary on stdout.
"""
from __future__ import annotations

import json
import sys

from backend.voice.reconcile import reconcile_recent_calls


def main(argv: list[str] | None = None) -> int:
    summary = reconcile_recent_calls()

    # Autonomous self-audit (Framework directive: turn the manual post-batch
    # call audit into a standing capability). This sweep already runs on a
    # schedule, so piggy-backing the audit here means every batch is audited,
    # trended, and P0-regression-alerted with NO operator invocation. Best-
    # effort — a self-audit failure must never disturb reconciliation.
    try:
        from backend.voice.call_batch_analyzer import autonomous_audit

        audit = autonomous_audit()
        if audit is not None:
            m = audit.metrics
            summary["audit"] = {
                "calls": m.total_calls,
                "engagement_rate": m.engagement_rate,
                "reasoning_leak_count": m.reasoning_leak_count,
                "finding_leaked_to_gatekeeper_count": m.finding_leaked_to_gatekeeper_count,
                "regression": bool(audit.trend.regressions),
            }
    except Exception:  # noqa: BLE001 — audit is additive, never blocks reconcile
        pass

    # Email channel outcome audit (deliverability/reputation) — the parallel
    # capability. Runs here so both channels self-audit on the same sweep.
    try:
        from backend.outreach.email_batch_analyzer import (
            autonomous_audit as email_audit,
            reputation_alerts,
        )

        em = email_audit()
        if em is not None:
            summary["email_audit"] = {
                "requests": em.requests,
                "delivered": em.delivered,
                "bounce_rate": em.bounce_rate,
                "spam_reports": em.spam_reports,
                "reputation_alerts": len(reputation_alerts(em)),
            }
    except Exception:  # noqa: BLE001 — additive, never blocks reconcile
        pass

    # HOTL close -> checkout handoff: queue a ready-to-send checkout draft for any
    # NEW closed call, so a verbal close becomes an operator one-click send within
    # minutes. Never auto-sends (financial/outward stays operator-gated).
    try:
        from backend.voice.close_handoff import scan_recent

        closes = scan_recent()
        if closes:
            summary["closes_queued"] = [
                {"company": c.company, "product": c.product_name,
                 "price": c.product_price, "needs_email": c.needs_email}
                for c in closes
            ]
    except Exception:  # noqa: BLE001 — additive, never blocks reconcile
        pass

    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
