"""Morning briefing CLI for the Hustleforge operator.

One-screen daily situational awareness combining the finance workcell with
live Stripe data. Surfaces in this order (most-urgent first):

  1. CRITICAL  -- today's debt-remediation actions + critical info gaps
  2. CASH      -- Stripe balance + MRR + active subs + CalFresh + runway
                 + cash-distress flag + MRR-adds (7d webhook-sourced)
  3. DEBT      -- structured-debt portfolio by tier (T1/T2/T3)
  4. SALES     -- today's prospecting call list + outreach follow-ups due
                 + live payment links + recent payments (auto-fulfill
                 rollup + url hints) + upsell nurture summary + voice
                 call summary
  5. GAPS      -- open info-gap counts by priority

Run from Samus venv:

    python -m backend.morning           # full briefing
    python -m backend.morning --no-color    # plain text for piping / logs

The companion PowerShell wrapper (``scripts/Show-Morning.ps1``) pulls
STRIPE_API_KEY from DPAPI before invoking this module so the live Stripe
roundtrip works without leaking secrets into shell history.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from backend.common.config import get_settings
from backend.finance.service import (
    get_actions_summary,
    get_codb_summary,
    get_debt_portfolio,
    get_declines_summary,
    get_hardship_context,
    get_info_gaps_summary,
    get_mrr_adds,
    get_payment_links,
    get_recent_payments,
    get_runway,
    get_upsell_summary,
)
from backend.finance.stripe_client import (
    StripeClient,
    StripeError,
    monthly_recurring_revenue_usd,
)
from backend.voice.service import get_voice_call_summary


# -- -------------------------------------------------------------------------
# Color helpers -- ANSI escapes when stdout is a TTY and NO_COLOR is unset
# -- -------------------------------------------------------------------------


def _color_on() -> bool:
    if os.getenv("SAMUS_MORNING_NO_COLOR"):
        return False
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _color_on() else text


def red(t: str) -> str:
    return _c(t, "31")


def green(t: str) -> str:
    return _c(t, "32")


def yellow(t: str) -> str:
    return _c(t, "33")


def cyan(t: str) -> str:
    return _c(t, "36")


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


# -- -------------------------------------------------------------------------
# Live data pulls
# -- -------------------------------------------------------------------------


def _fetch_stripe_cash() -> tuple[float, float, int, str | None]:
    """Return (available_usd, mrr_usd, active_sub_count, error_or_none)."""
    settings = get_settings()
    key = (settings.stripe_api_key or "").strip()
    if not key:
        return 0.0, 0.0, 0, "stripe_api_key_unset"
    client = StripeClient(api_key=key)
    try:
        balance = client.fetch_balance()
        subs = client.fetch_subscriptions(status="active", limit=100)
    except StripeError as exc:
        return 0.0, 0.0, 0, str(exc)
    return (
        balance.available_usd_dollars(),
        monthly_recurring_revenue_usd(subs),
        len(subs),
        None,
    )


def _today_call_list_path(today: date) -> Path | None:
    try:
        from backend.common import storage

        return storage.root() / "daily_calls" / f"morning_call_list_{today.isoformat()}.txt"
    except Exception:  # noqa: BLE001
        return None


def _today_call_list_csv(today: date) -> Path | None:
    """Path to the structured ``call_list_<date>.csv`` the prospecting run writes."""
    try:
        from backend.common import storage

        return storage.root() / "daily_calls" / f"call_list_{today.isoformat()}.csv"
    except Exception:  # noqa: BLE001
        return None


def _render_call_list(today: date) -> list[str]:
    """Render the SALES call-list block — one scannable line per prospect.

    Reads the structured ``call_list_<date>.csv`` written by the prospecting
    pipeline. The brief itself is the call list now: priority, name, phone,
    location, scores. Full per-prospect call scripts + the website
    investigation stay in the attached ``morning_call_list_<date>.txt``.
    """
    import csv as _csv

    csv_path = _today_call_list_csv(today)
    if csv_path is None or not csv_path.exists():
        return [
            yellow("  !  no call list for today"),
            dim("     generate via the prospecting workcell or rerun later"),
        ]
    try:
        with csv_path.open(encoding="utf-8", newline="") as fh:
            rows = list(_csv.DictReader(fh))
    except OSError:
        return [yellow("  !  today's call list could not be read")]
    if not rows:
        return [yellow("  !  today's call list is empty — 0 prospects discovered")]

    rank = {"hot": 0, "warm": 1, "low": 2}
    dot = {"hot": "🔴", "warm": "🟡", "low": "🟢"}
    # Security grade tie-breaker — worse grade ranks higher; ungraded last.
    grade_rank = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4}

    def _num(value: str | None) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def _priority(row: dict[str, str]) -> str:
        return (row.get("call_priority") or "low").lower()

    rows.sort(
        key=lambda r: (
            rank.get(_priority(r), 9),
            -_num(r.get("lead_score")),
            grade_rank.get((r.get("security_grade") or "").upper(), 9),
            _num(r.get("seo_score")),
        )
    )
    counts = {"hot": 0, "warm": 0, "low": 0}
    for r in rows:
        counts[_priority(r)] = counts.get(_priority(r), 0) + 1

    out: list[str] = [
        green(
            f"  {len(rows)} prospects ready — "
            f"{counts['hot']} hot, {counts['warm']} warm, {counts['low']} low"
        )
    ]
    for idx, r in enumerate(rows, start=1):
        name = (r.get("company_name") or "(unnamed)")[:32]
        phone = r.get("phone") or "—"
        loc = r.get("city") or "—"
        industry = r.get("industry") or "—"
        owner = r.get("owner_name") or ""
        sec = (r.get("security_grade") or "").strip()
        sec_tag = f" · sec {sec}" if sec else ""
        line = (
            f"  {dot.get(_priority(r), '⚪')} {idx:>2}. {name:<32}  "
            f"{phone:<16}  {loc} · {industry}  "
            f"[lead {_num(r.get('lead_score'))} · seo {_num(r.get('seo_score'))}{sec_tag}]"
        )
        if owner:
            line += f"  — ask for {owner}"
        out.append(line)
    out.append(
        dim(
            "     full call scripts + website investigation: "
            f"attached morning_call_list_{today.isoformat()}.txt"
        )
    )
    return out


def _render_logged_calls(today: date) -> list[str]:
    """Yesterday's hand-logged call outcomes, from the operator journal.

    Reads ``call_outcomes_<yesterday>.jsonl`` (written by Log-Call.ps1 /
    backend.crm.log_call). Returns [] when nothing was logged, so the SALES
    section simply omits the block. Booked + follow-up calls — the ones that
    need action today — are listed with their notes; the rest are counted.
    """
    import json as _json
    from datetime import timedelta

    from backend.common import storage

    yesterday = today - timedelta(days=1)
    path = storage.root() / "daily_calls" / f"call_outcomes_{yesterday.isoformat()}.jsonl"
    if not path.exists():
        return []
    try:
        records = [
            _json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError):
        return []
    if not records:
        return []

    counts: dict[str, int] = {}
    for r in records:
        oc = str(r.get("outcome") or "?")
        counts[oc] = counts.get(oc, 0) + 1
    breakdown = ", ".join(f"{n} {oc}" for oc, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    out: list[str] = [green(f"  Calls logged yesterday: {len(records)}  -  {breakdown}")]
    for r in records:
        oc = str(r.get("outcome") or "")
        if oc not in ("booked", "follow_up"):
            continue
        company = str(r.get("company") or "").strip()[:44]
        note = str(r.get("notes") or "").strip()
        tag = "BOOKED   " if oc == "booked" else "follow up"
        head = f"    {tag}  {company}"
        out.append(green(head) if oc == "booked" else head)
        if note:
            out.append(dim(f"       {note[:160]}"))
    return out


def _render_follow_ups(today: date) -> list[str]:
    """Outreach follow-ups due — prospects emailed earlier, now due a call.

    Reads CRM CallState via ``crm.service.list_follow_ups_due`` (a DynamoDB
    scan). Best-effort: any failure — AWS creds absent, DynamoDB unreachable,
    the CRM package not importable — degrades to an omitted section rather
    than breaking the brief, the same posture as the Stripe pulls. Returns []
    when there are no follow-ups, so the SALES section just omits the block.
    """
    try:
        from backend.crm import service as crm_service

        result = crm_service.list_follow_ups_due(today=today.isoformat())
    except Exception:  # noqa: BLE001 — CRM is an optional input to the brief
        return []
    if result.ddb_error or not result.follow_ups:
        return []

    out: list[str] = [
        green(f"  Follow-ups due: {result.count}  (outreach sent earlier — time to call)")
    ]
    for fu in result.follow_ups:
        company = (fu.company or fu.prospect_id)[:32]
        phone = fu.phone or "—"
        if fu.days_waiting <= 0:
            waited = "emailed today"
        elif fu.days_waiting == 1:
            waited = "emailed 1d ago"
        else:
            waited = f"emailed {fu.days_waiting}d ago"
        line = f"  📨 {company:<32}  {phone:<16}  {waited}"
        if fu.subject:
            line += f'  · "{fu.subject[:48]}"'
        out.append(line)
        # Suggested "second opportunity" — a catalog upsell the prospect's
        # prior interest signals point to. Omitted when there's no clear signal.
        if fu.upsell_pitch:
            out.append(dim(f"       ↗ {fu.upsell_pitch}"))
    return out


def _render_command_center(today: date) -> list[str]:
    """COMMAND CENTER summary — the top-of-brief executive digest.

    HOTL Tranche 4. Renders FROM the same aggregate the ``GET
    /admin/command_center`` endpoint returns (``planning.command_center``), so
    the brief and the API are one source of truth. Best-effort like every other
    brief input: any failure degrades to an omitted section rather than
    breaking the brief. Returns [] when there is nothing worth surfacing.
    """
    try:
        from backend.planning.command_center import build_command_center

        agg = build_command_center(day=today.isoformat())
    except Exception:  # noqa: BLE001 — command center is an optional input
        return []
    if not isinstance(agg, dict) or not agg.get("ok"):
        return []

    lines: list[str] = [bold("COMMAND CENTER")]

    # -- what's running now (active plans + open tasks) ----------------------
    running = agg.get("running_now") or {}
    plan_count = int(running.get("plan_count") or 0)
    goal_count = int(running.get("goal_count") or 0)
    open_tasks = int(running.get("open_task_count") or 0)
    lines.append(
        f"  Plans active: {plan_count}   Goals tracked: {goal_count}   Open tasks: {open_tasks}"
    )

    # -- what needs approval --------------------------------------------------
    needs = agg.get("needs_approval") or {}
    pending = int(needs.get("pending_count") or 0)
    emergency = int(needs.get("emergency_count") or 0)
    if pending:
        note = f"  Awaiting approval: {yellow(str(pending))}"
        if emergency:
            note += red(f"  ({emergency} EMERGENCY)")
        lines.append(note)
    else:
        lines.append(dim("  Awaiting approval: 0"))

    # -- why (most recent decision) ------------------------------------------
    why = agg.get("why") or {}
    decisions = why.get("decisions") or []
    if decisions:
        d0 = decisions[0]
        actor = str(d0.get("actor") or "")
        reason = str(d0.get("why") or "")[:64]
        lines.append(dim(f"  Latest decision [{actor}]: {reason}"))

    # -- health ---------------------------------------------------------------
    health = (agg.get("health") or {}).get("health") or {}
    state = str(health.get("state") or "")
    if state:
        palette = {
            "ok": green,
            "degraded": yellow,
            "awaiting_operator_action": yellow,
            "fail": red,
        }
        color = palette.get(state, lambda x: x)
        lines.append(f"  System health: {color(state.upper())}")

    # -- what happened (24h event digest) ------------------------------------
    happened = agg.get("what_happened") or {}
    ev_count = int(happened.get("event_count") or 0)
    if ev_count:
        by_type = happened.get("by_type") or {}
        top = sorted(by_type.items(), key=lambda kv: -int(kv[1]))[:4]
        summary = ", ".join(f"{k.split('.')[-1]} {v}" for k, v in top)
        lines.append(dim(f"  Last 24h: {ev_count} events ({summary})"))

    lines.append("")
    return lines


def _render_economics(today: date) -> list[str]:
    """ECONOMICS section — daily ROI roll-up + top-10 arbitrated work queue.

    HOTL Tranche 2 surfaces. Best-effort like every other brief input: any
    failure (stores unreadable, modules unimportable) degrades to an omitted
    section rather than breaking the brief. Returns [] when there is nothing
    to show.
    """
    lines: list[str] = []
    # -- ROI roll-up ----------------------------------------------------------
    try:
        from backend.finance.roi import get_rollup

        rollup = get_rollup(today.isoformat())
    except Exception:  # noqa: BLE001 — economics is an optional input
        rollup = None
    if rollup:
        rev = float(rollup.get("revenue_usd") or 0.0)
        cost = float(rollup.get("cost_usd") or 0.0)
        net = float(rollup.get("net_usd") or 0.0)
        if rev or cost:
            lines.append(bold("ECONOMICS"))
            net_color = green if net >= 0 else red
            lines.append(
                f"  Revenue today:        {_fmt_usd(rev)}   "
                f"Cost: {_fmt_usd(cost)}   Net: {net_color(_fmt_usd(net))}"
            )
            by_channel = rollup.get("by_channel") or {}
            for channel, row in sorted(
                by_channel.items(),
                key=lambda kv: float(kv[1].get("net_usd") or 0.0),
                reverse=True,
            )[:5]:
                lines.append(
                    dim(
                        f"     {channel:<12} rev ${float(row.get('revenue_usd') or 0):.2f}"
                        f"  cost ${float(row.get('cost_usd') or 0):.2f}"
                        f"  net ${float(row.get('net_usd') or 0):.2f}"
                    )
                )
    # -- arbitrated queue -------------------------------------------------------
    try:
        from backend.strategy.arbiter import latest_arbitration

        arb = latest_arbitration()
    except Exception:  # noqa: BLE001
        arb = None
    ranked = (arb or {}).get("ranked") or []
    if ranked:
        if not lines:
            lines.append(bold("ECONOMICS"))
        best = (arb or {}).get("best_channel") or ""
        header = f"  Today's ranked queue (top {min(10, len(ranked))} of {len(ranked)})"
        if best:
            header += f"  · bandit-preferred channel: {best}"
        lines.append(green(header))
        for row in ranked[:10]:
            action = str(row.get("action") or "")[:24]
            target = str(row.get("target_id") or "")[:20]
            lines.append(
                f"  #{int(row.get('rank') or 0):<3} {action:<24} {target:<20}"
                f"  EV ${float(row.get('ev_usd') or 0):>9,.2f}"
                f"  p={float(row.get('probability') or 0):.2f}"
                f"  prio {float(row.get('priority') or 0):,.1f}"
            )
            why = str(row.get("rationale") or "")
            if why:
                lines.append(dim(f"       {why[:68]}"))
    if lines:
        lines.append("")
    return lines


def _render_system_health(today: date) -> list[str]:
    """SYSTEM HEALTH section (HOTL T5) — open diagnostics + weakest workcells.

    Best-effort like every other brief input: any failure (modules
    unimportable, ledgers unreadable) degrades to an omitted section rather than
    breaking the brief. Detection-only here (remediate=False) — the brief must
    never trigger a requeue/restart as a side effect of being rendered. Returns
    [] when there is nothing noteworthy to show.
    """
    lines: list[str] = []
    # -- open diagnostic findings --------------------------------------------
    try:
        from backend.entropy.diagnostics import run_diagnostics

        report = run_diagnostics(remediate=False)
    except Exception:  # noqa: BLE001 — health is an optional input
        report = None
    findings = (report or {}).get("findings") or []
    if findings:
        lines.append(bold("SYSTEM HEALTH"))
        counts = (report or {}).get("counts") or {}
        lines.append(
            red(
                f"  Diagnostics: {counts.get('critical', 0)} critical, {counts.get('warn', 0)} warn"
            )
        )
        for f in findings[:6]:
            sev = str(f.get("severity") or "")
            paint = red if sev == "critical" else yellow
            lines.append(paint(f"  ! [{f.get('detector')}] {str(f.get('detail'))[:60]}"))
            if f.get("remediation"):
                lines.append(dim(f"       remediation: {f.get('remediation')}"))
    # -- weakest workcells by reputation -------------------------------------
    try:
        from backend.common.reputation import get_reputation

        rep = get_reputation(recompute=True, day=today.isoformat())
    except Exception:  # noqa: BLE001
        rep = None
    workcells = (rep or {}).get("workcells") or {}
    # Surface only genuinely weak ones (score < 0.9) with real evidence.
    weak = sorted(
        (
            r
            for r in workcells.values()
            if float(r.get("score") or 1.0) < 0.9 and int(r.get("sample_size") or 0) > 0
        ),
        key=lambda r: float(r.get("score") or 1.0),
    )[:5]
    if weak:
        if not lines:
            lines.append(bold("SYSTEM HEALTH"))
        lines.append(yellow("  Lowest-reputation workcells:"))
        for r in weak:
            lines.append(
                dim(
                    f"     {str(r.get('workcell')):<12} score {float(r.get('score') or 0):.2f}"
                    f"  success {float(r.get('success_rate') or 0):.2f}"
                    f"  reliab {float(r.get('reliability') or 0):.2f}"
                    f"  net ${float(r.get('profitability_usd') or 0):.2f}"
                )
            )
    if lines:
        lines.append("")
    return lines


def _render_production_health(today: date) -> list[str]:
    """PRODUCTION HEALTH section - autonomous-loop failure watchdog.

    Surfaces the checks from backend.observability.production_health (Gmail
    OAuth token expiry, inbox-poll exit code, and the outbound-live-while-
    inbound-blind asymmetry that risks emailing opted-out recipients). Only
    alert-level findings (WARN / FAIL / CRITICAL) render; a clean loop shows
    nothing. Best-effort like every other brief input - any failure degrades to
    an omitted section rather than breaking the brief, and the checks are
    read-only (they never actuate production)."""
    try:
        from backend.observability.production_health import check_production_health

        report = check_production_health()
    except Exception:  # noqa: BLE001 - health is an optional input
        return []
    if not getattr(report, "enabled", False):
        return []
    alerts = report.alerts()
    if not alerts:
        return []
    lines: list[str] = [bold("PRODUCTION HEALTH")]
    for c in alerts:
        paint = red if c.status in ("FAIL", "CRITICAL") else yellow
        lines.append(paint(f"  [{c.status}] {c.name}: {c.detail}"))
        if c.remediation:
            lines.append(dim(f"       fix: {c.remediation}"))
    lines.append("")
    return lines


# -- -------------------------------------------------------------------------
# Formatter
# -- -------------------------------------------------------------------------

SEP = "=" * 75
RULE = "-" * 75


def _fmt_usd(amount: float, width: int = 12) -> str:
    """Right-pad the whole '$1,234.56' string so columns line up."""
    return f"${amount:,.2f}".rjust(width)


def _render_autonomous_gate(today: date) -> list[str]:
    """Render the ADR-018 autonomous-day gate status.

    Attestation is the operator's daily acknowledgement that unlocks autonomous
    B2B live dialing. Absent -> every cold prospect routes to voicemail draft
    (no live dial). This section is the FIRST thing in the brief so the operator
    sees the day's gate status the moment they open the email.

    Fail-OPEN: any read fault renders a warning stub rather than blocking the
    briefing. The stub still tells the operator how to attest by hand.
    """
    try:
        from backend.common.preshift_attestation import is_attested
    except Exception:  # noqa: BLE001
        return [red(bold("AUTONOMOUS DAY GATE -- unknown (preshift module unavailable)")), ""]
    try:
        attested = bool(is_attested())
    except Exception:  # noqa: BLE001
        attested = False
    lines: list[str] = []
    if attested:
        lines.append(
            green(
                bold(
                    "AUTONOMOUS DAY GATE -- ATTESTED (autonomous B2B live dial UNLOCKED for today)"
                )
            )
        )
        lines.append(
            dim(
                "  Governed dial fences (call-hours / DNC / cooldown / cap) still apply per prospect."
            )
        )
    else:
        lines.append(
            red(bold("AUTONOMOUS DAY GATE -- NOT ATTESTED (autonomous live dial WILL NOT FIRE)"))
        )
        lines.append(red("  Every cold prospect routes to VOICEMAIL DRAFT instead of a live call."))
        lines.append("")
        lines.append(bold("  ATTEST NOW (one command, unlocks the day):"))
        lines.append(
            dim(
                "    Invoke-RestMethod -Method Post -Uri "
                "http://127.0.0.1:8100/api/gateway/pre_shift_briefing -TimeoutSec 60"
            )
        )
        lines.append(
            dim(
                "  (ADR-018: running the pre-shift briefing IS your acknowledgement of "
                "the day's autonomous execution.)"
            )
        )
    lines.append("")
    return lines


def _render_production_readiness(today: date) -> list[str]:
    """Render TODAY'S production-readiness verdict for the four autonomous lanes.

    Purpose: catch silent-failure days like 2026-07-06 (dial=0 for 4 straight
    days, list starved to 7 prospects, one silent gate) before they happen.
    Each check is a single line + a clear PASS/WARN/FAIL. Fail-OPEN on every
    read: an internal fault shows a "?" rather than lying about readiness.

    Ordered by prod-blocker severity: dial list first (no list -> no calls no
    matter what), then dial-eligible count, then email lane, then Apollo
    posture. Everything the operator needs to know AT A GLANCE whether the day
    can produce.
    """
    lines: list[str] = []
    lines.append(bold(f"PRODUCTION READINESS -- {today.isoformat()}"))

    # (1) Dial list — the day-shape input for every other autonomous action.
    try:
        from backend.common import storage

        csv_path = storage.root() / "daily_calls" / f"call_list_{today.isoformat()}.csv"
        if csv_path.is_file():
            import csv as _csv

            with csv_path.open(encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
            n_total = len(rows)
            n_phone = sum(1 for r in rows if (r.get("phone") or "").strip())
            n_email = sum(1 for r in rows if (r.get("owner_email") or "").strip())
            n_stake_ready = sum(
                1
                for r in rows
                if (r.get("owner_email") or "").strip() and int(r.get("lead_score") or 0) >= 70
            )
            if n_total == 0:
                lines.append(
                    red(f"  X  dial list:       EMPTY ({csv_path.name}) -- self-supply starved")
                )
            elif n_total < 15:
                lines.append(
                    yellow(f"  !  dial list:       {n_total} prospects (THIN; typical day is 60+)")
                )
            else:
                lines.append(green(f"  ok dial list:       {n_total} prospects"))
            if n_phone < n_total:
                lines.append(dim(f"     phone-dialable:  {n_phone}/{n_total}"))
            lines.append(dim(f"     email-eligible:  {n_email}/{n_total}"))
            lines.append(dim(f"     auto-stake ready (email + score>=70): {n_stake_ready}"))
        else:
            lines.append(
                red(
                    f"  X  dial list:       MISSING ({csv_path.name}) -- prospecting daily_supply did not fire"
                )
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(dim(f"  ?  dial list:       read failed ({exc})"))

    # (2) Email composer — one of the two send lanes. sendgrid key absent =
    # every send-attempt fails-closed. Check the env the running processes see.
    import os as _os

    sg_key = (_os.environ.get("SENDGRID_API_KEY") or "").strip()
    sg_from = (_os.environ.get("SENDGRID_FROM_EMAIL") or "").strip()
    if sg_key and sg_from:
        lines.append(green(f"  ok email lane:      SendGrid armed (from {sg_from})"))
    elif sg_key:
        lines.append(
            yellow(
                "  !  email lane:      key armed but SENDGRID_FROM_EMAIL unset -- sends will fail"
            )
        )
    else:
        lines.append(
            red("  X  email lane:      SendGrid key not visible in env -- outreach disabled")
        )

    # (3) Apollo posture (last-resort enrichment). Not fatal when disabled; the
    # free cascade covers primary enrichment. But an operator seeing "?/2540 credits"
    # can react before it's silently zero. Cheap check: read the local budget store.
    try:
        from backend.common.apollo_budget import get_credit_ceiling, get_store

        try:
            budget = get_store().load()
            spent = float(getattr(budget, "spent_today_usd", 0.0) or 0.0)
            lifetime = int(getattr(budget, "lifetime_credits", 0) or 0)
            ceiling = get_credit_ceiling()
            if ceiling and lifetime >= ceiling:
                lines.append(
                    red(
                        f"  X  apollo:          credit ceiling hit ({lifetime}/{ceiling}) -- adapter disabled"
                    )
                )
            elif spent > 0:
                lines.append(green(f"  ok apollo:          today spend ${spent:.2f}"))
            else:
                lines.append(dim("     apollo:          idle today (free cascade primary)"))
        except Exception:  # noqa: BLE001 — budget module present but store faulted
            lines.append(dim("     apollo:          budget store not yet initialized"))
    except ImportError:
        # apollo_budget not present on this branch (pre-PR#8). Skip cleanly.
        pass
    except Exception:  # noqa: BLE001
        pass

    lines.append("")
    return lines


def _render_codex_drafts() -> list[str]:
    """Render the 'Open Codex ADR drafts' header section.

    Fail-OPEN: if the codex dir is missing / unreadable, return [] so the
    brief continues without this section. Lists each draft with name, the
    rule that triggered it (parsed from the draft header), and age in
    hours.
    """
    import re as _re
    import time as _time

    try:
        from backend.common.codex.registry import _default_codex_dir
    except Exception:  # noqa: BLE001
        return []
    try:
        drafts_dir = _default_codex_dir() / "_drafts"
    except Exception:  # noqa: BLE001
        return []
    if not drafts_dir.is_dir():
        return []
    try:
        drafts = sorted(drafts_dir.glob("ADR-*.draft.md"))
    except OSError:
        return []
    if not drafts:
        return []
    now = _time.time()
    rule_re = _re.compile(r"blocked by\s+([A-Za-z0-9-]+)")
    lines: list[str] = []
    lines.append(red(bold(f"OPEN CODEX ADR DRAFTS ({len(drafts)})")))
    for path in drafts:
        try:
            head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            rule = "(unknown rule)"
            if head:
                m = rule_re.search(head[0])
                if m:
                    rule = m.group(1)
            age_h = max(0.0, (now - path.stat().st_mtime) / 3600.0)
        except OSError:
            rule = "(unknown rule)"
            age_h = 0.0
        lines.append(red(f"  *  {path.name}  ") + dim(f"rule={rule}  age={age_h:.1f}h"))
    lines.append(dim("     resolve via /api/console/codex/drafts/<name>/resolve"))
    lines.append("")
    return lines


def _render_guidance_laws(limit: int = 3) -> list[str]:
    """Render the 'Guidance laws' section (Concept 2 -- Strategic Compression).

    Top-N durable strategy rules from the compression rollup -- business laws
    the operator can act on without re-deriving them. Fail-OPEN: returns []
    when the rollup is missing or empty. Newest laws first so the freshest
    institutional learning surfaces at the top of the section.
    """
    try:
        from backend.experiments.promoter import read_guidance_laws
    except Exception:  # noqa: BLE001
        return []
    try:
        laws = read_guidance_laws(limit=max(limit * 4, 50))
    except Exception:  # noqa: BLE001
        return []
    if not laws:
        return []
    laws = list(reversed(laws))[:limit]
    lines: list[str] = []
    lines.append(bold(f"GUIDANCE LAWS (top {len(laws)})"))
    for law in laws:
        conf_pct = int(round(max(0.0, min(1.0, law.confidence)) * 100))
        lines.append(f"  *  {law.law}")
        cat = law.category or "n/a"
        lines.append(
            dim(f"       evidence={law.evidence_count}  confidence={conf_pct}%  category={cat}")
        )
    lines.append("")
    return lines


def _samus_saturation_reader() -> dict[str, float]:
    """Trials-by-dimension -> saturation-risk map (Samus-side supplier for
    :func:`compute_organizational_economics`).

    Samus's `vertical` in the org-economics sense is an experiment dimension
    (pricing_tier / call_script / cadence / template / ...): each dimension is
    a discrete arm-of-arms that competes for reasoning attention. Trial counts
    per dimension come from the attribution store via
    :func:`experiments.registry.arm_stats`, summed across every arm of every
    active experiment on that dimension. Archived experiments are excluded so
    the risk map reflects present exploration load, not history.

    Fail-OPEN: an empty result (no active experiments) yields ``{}`` which the
    report renders as "no verticals reported" - not degraded, just quiet.
    Any exception yields ``{}`` too rather than crashing the brief.
    """
    try:
        from backend.experiments.registry import (
            arm_stats,
            list_experiments,
        )
        from backend.strategy.saturation_monitor import (
            saturation_risk_by_vertical,
        )
    except Exception:  # noqa: BLE001
        return {}
    trials_by_dim: dict[str, float] = {}
    try:
        experiments = list_experiments(status="active")
    except Exception:  # noqa: BLE001
        return {}
    for exp in experiments:
        dim = (getattr(exp, "dimension", "") or "unknown").strip() or "unknown"
        try:
            stats = arm_stats(getattr(exp, "experiment_id", "")) or {}
        except Exception:  # noqa: BLE001
            continue
        total = 0.0
        for row in stats.values():
            try:
                total += float(row.get("trials") or 0)
            except (TypeError, ValueError):
                continue
        if total <= 0.0:
            continue
        trials_by_dim[dim] = trials_by_dim.get(dim, 0.0) + total
    if not trials_by_dim:
        return {}
    try:
        return saturation_risk_by_vertical(trials_by_dim)
    except Exception:  # noqa: BLE001
        return {}


def _render_organizational_economics(now_ts: float | None = None) -> list[str]:
    """Render the 'Organizational economics' section (Concept 4).

    Six-metric joined report over org_debt + regret_engine + saturation_monitor
    + approvals. Fail-OPEN: returns [] when the report is disabled or empty.
    Degraded readers (sources_missing=True) still surface, marked so the
    operator can see which sources are dark rather than silently reading 0.

    ``saturation_reader`` is supplied via :func:`_samus_saturation_reader` so
    the cognitive-overhead metric reflects real per-dimension exploration
    load. ``regret_reader`` remains unsupplied because ``RegretLedger`` has
    no persistence layer today (per-arm token spend telemetry is
    tier-3-deferred in ``backend.strategy.portfolio_manager``); passing a
    hardcoded zero would fake resolution when the truth is 'no data yet'.
    """
    try:
        from backend.observability.organizational_economics_report import (
            compute_organizational_economics,
        )
    except Exception:  # noqa: BLE001
        return []
    try:
        report = compute_organizational_economics(
            now_ts=now_ts,
            saturation_reader=_samus_saturation_reader,
        )
    except Exception:  # noqa: BLE001
        return []
    if not report.enabled or not report.metrics:
        return []
    lines: list[str] = []
    degraded = sum(1 for m in report.metrics if m.sources_missing)
    tag = f"  ({degraded} degraded)" if degraded else ""
    lines.append(bold(f"ORGANIZATIONAL ECONOMICS{tag}"))
    for m in report.metrics:
        marker = red("  !") if m.sources_missing else "   "
        lines.append(f"{marker}  {m.name.ljust(22)}  {m.value:>10.3f}  {dim(m.unit)}")
        lines.append(dim(f"          {m.detail}"))
    lines.append("")
    return lines


def render_briefing(*, today: date | None = None) -> str:
    """Build the full briefing text. Pure-ish -- pulls live data but no I/O writes."""
    today = today or date.today()

    actions = get_actions_summary()
    info_gaps = get_info_gaps_summary()
    debt = get_debt_portfolio()
    codb = get_codb_summary()
    declines = get_declines_summary(window_days=30)
    runway = get_runway()
    hardship = get_hardship_context()
    available_usd, mrr_usd, sub_count, stripe_err = _fetch_stripe_cash()

    lines: list[str] = []
    lines.append(SEP)
    lines.append(bold(f"SAMUS MORNING BRIEFING -- {today.strftime('%A, %B %d, %Y')}"))
    lines.append(SEP)
    lines.append("")

    # -- -------- 0. AUTONOMOUS DAY GATE (ADR-018 attestation status) --------
    # First section by design: if the gate is closed, autonomous production is
    # inert regardless of everything below, and the operator needs to see that
    # before scanning the rest of the brief.
    lines.extend(_render_autonomous_gate(today))

    # -- -------- 0b. PRODUCTION READINESS (dial list + email + apollo) -----
    # Second section: catch silent-failure days (2026-07-06 pattern: attested
    # but list starved to 7 prospects). PASS/WARN/FAIL for each of the four
    # autonomous lanes so an unhealthy day is obvious in the first screen.
    lines.extend(_render_production_readiness(today))

    # -- -------- 0a. COMMAND CENTER (HOTL T4 executive digest) --------------
    # Top-of-brief summary rendered from the SAME aggregate the
    # /admin/command_center endpoint serves (one source of truth).
    command_center_section = _render_command_center(today)
    if command_center_section:
        lines.extend(command_center_section)

    # -- -------- 0. CODEX DRAFTS (leads if any are open) -- -----------------
    codex_section = _render_codex_drafts()
    if codex_section:
        lines.extend(codex_section)

    # -- -------- 0d. GUIDANCE LAWS (Concept 2 -- compressed strategy) -------
    lines.extend(_render_guidance_laws())

    # -- -------- 1. CRITICAL -- ----------------------------------------------
    crit_count = actions.due_today_count + len(info_gaps.critical_open)
    lines.append(
        red(
            bold(
                f"CRITICAL -- TODAY ({actions.due_today_count} actions, "
                f"{len(info_gaps.critical_open)} critical gaps, "
                f"{actions.overdue_count} overdue)"
            )
        )
    )
    if actions.overdue_count:
        lines.append("")
        lines.append(red("  ! OVERDUE -- address first"))
        for a in actions.overdue_actions[:5]:
            lines.append(red(f"  X  {a.due_date}  {a.action}"))
            if a.where:
                lines.append(dim(f"       where: {a.where}"))
    if actions.due_today_count:
        lines.append("")
        for a in actions.due_today_actions:
            lines.append(red(f"  ->  {a.action}"))
            if a.where:
                lines.append(dim(f"       where: {a.where}"))
    elif actions.overdue_count == 0:
        lines.append(dim("  (no actions due today)"))
    if info_gaps.critical_open:
        lines.append("")
        lines.append(red("  Open CRITICAL info gaps:"))
        for g in info_gaps.critical_open:
            tag = f" [{g.related_debt_id}]" if g.related_debt_id else ""
            lines.append(red(f"  *  {g.gap}{tag}"))
            lines.append(dim(f"       how: {g.how_to_close}"))
    lines.append("")

    # -- -------- 2. CASH -- --------------------------------------------------
    lines.append(bold("CASH"))
    if stripe_err:
        lines.append(
            f"  Available (Stripe):   {_fmt_usd(available_usd)}  {dim('(' + stripe_err + ')')}"
        )
        lines.append(f"  MRR (active subs):    {dim('unavailable')}")
    else:
        lines.append(f"  Available (Stripe):   {_fmt_usd(available_usd)}")
        mrr_str = _fmt_usd(mrr_usd)
        sub_str = f"({sub_count} active subscription{'s' if sub_count != 1 else ''})"
        lines.append(f"  MRR (active subs):    {green(mrr_str)}  {dim(sub_str)}")
    cf_monthly = hardship.calfresh.recurring_benefits_usd or 0.0
    if hardship.calfresh.approved and cf_monthly:
        lines.append(f"  CalFresh monthly:     {_fmt_usd(cf_monthly)}")
    lines.append(f"  Monthly burn (CODB):  {_fmt_usd(codb.total_monthly_burn_usd)}")
    if runway.days_of_runway is None:
        runway_text = "inf (zero burn)"
        runway_color = green
    else:
        runway_text = f"{runway.days_of_runway:.1f} days"
        runway_color = red if runway.alert_triggered else green
    lines.append(f"  Days of runway:       {runway_color(runway_text.rjust(11))}")
    distress_palette = {"ok": green, "degraded": yellow, "critical": red}
    distress_color = distress_palette.get(declines.cash_distress, lambda x: x)
    lines.append(
        f"  Cash distress flag:   {distress_color(declines.cash_distress.upper().rjust(11))}"
    )
    for reason in declines.distress_reasons[:3]:
        lines.append(dim(f"     - {reason}"))
    # MRR added in the last 7d from new subscription webhooks. Surfaced
    # alongside (not replacing) the live-Stripe MRR line above so the operator
    # can spot fresh upsell conversions even before Stripe's subs endpoint
    # reflects them.
    mrr_adds = get_mrr_adds(window_days=7)
    if mrr_adds.added_count > 0:
        amount_str = _fmt_usd(mrr_adds.total_mrr_usd)
        sub_word = "subscription" if mrr_adds.added_count == 1 else "subscriptions"
        lines.append(
            f"  MRR added (7d):       {green(amount_str)}  "
            f"{dim(f'({mrr_adds.added_count} {sub_word})')}"
        )
    lines.append("")

    # -- -------- 2b. ECONOMICS (HOTL T2: ROI roll-up + arbitrated queue) -----
    lines.extend(_render_economics(today))

    # -- -------- 2c. ORGANIZATIONAL ECONOMICS (Concept 4 -- energy waste) ---
    lines.extend(_render_organizational_economics())

    # -- SYSTEM HEALTH (HOTL T5) — diagnostics + weakest workcells ------------
    lines.extend(_render_system_health(today))

    # -- PRODUCTION HEALTH — autonomous-loop failure watchdog -----------------
    lines.extend(_render_production_health(today))

    # -- -------- 3. DEBT PORTFOLIO -- ---------------------------------------
    lines.append(bold("DEBT PORTFOLIO"))
    if not debt.registry_loaded:
        lines.append(dim("  (debts.yaml not populated)"))
    else:
        tier_label = {
            1: ("T1 legal exposure", red),
            2: ("T2 utilities", yellow),
            3: ("T3 unsecured", cyan),
        }
        for tier in debt.by_tier:
            label, color = tier_label.get(tier.tier, (f"T{tier.tier}", lambda x: x))
            unk = f" + {tier.unknown_balance_count} unknown" if tier.unknown_balance_count else ""
            lines.append(
                f"  {color(label.ljust(22))} {_fmt_usd(tier.confirmed_total_usd)}  "
                f"({tier.debt_count} debt{'s' if tier.debt_count != 1 else ''}{unk})"
            )
        lines.append(f"  {bold('TOTAL CONFIRMED:'.ljust(22))} {_fmt_usd(debt.confirmed_total_usd)}")
        if debt.unknown_balance_count:
            lines.append(
                dim(
                    f"  ({debt.unknown_balance_count} debt{'s' if debt.unknown_balance_count != 1 else ''} "
                    f"with unknown balances -- see /info_gaps)"
                )
            )
    lines.append("")

    # -- -------- 4. SALES / CALL LIST -- ------------------------------------
    lines.append(bold("SALES"))
    lines.extend(_render_call_list(today))
    follow_ups = _render_follow_ups(today)
    if follow_ups:
        lines.append("")
        lines.extend(follow_ups)
    logged = _render_logged_calls(today)
    if logged:
        lines.append("")
        lines.extend(logged)
    lines.append("")
    pl = get_payment_links(active=True)
    if not pl.stripe_reachable:
        lines.append(dim(f"  Live payment links: unavailable ({pl.stripe_error})"))
    else:
        header = (
            f"  Live payment links: {pl.count_total} active "
            f"({pl.count_one_time} one-time, {pl.count_subscription} subscription, "
            f"{pl.livemode_count} livemode)"
        )
        # Summary line only — the per-link URL list is operator noise; the
        # count + one-time/subscription/livemode breakdown is sufficient.
        # The full link inventory lives in Stripe / the catalog if needed.
        lines.append(dim(header))
    lines.append("")

    # -- -------- 4b. RECENT PAYMENTS (Stripe webhook) ---------------------
    payments = get_recent_payments(window_days=7)
    if payments.log_loaded:
        header = (
            f"  Recent payments (last {payments.window_days}d): "
            f"{payments.total_count} total "
            f"({payments.processed_count} processed, "
            f"{payments.ignored_count} ignored, "
            f"{payments.failed_count} failed)"
        )
        lines.append(dim(header))
        # Auto-fulfill rollup: surface successes as a one-line green
        # confirmation; surface failures separately and loudly so they don't
        # get lost in the awaiting bucket below.
        if payments.auto_fulfilled_count:
            lines.append(
                green(
                    f"  v  {payments.auto_fulfilled_count} auto-fulfilled "
                    f"(SEO deliverable + receipt sent without operator)"
                )
            )
        if payments.auto_fulfill_failed_count:
            lines.append(
                red(
                    f"  !  {payments.auto_fulfill_failed_count} auto-fulfill "
                    f"FAILED -- inspect stripe_events.jsonl + re-run manually"
                )
            )
        if payments.awaiting_fulfillment_count > 0:
            line = (
                f"  {yellow('->')} {payments.awaiting_fulfillment_count} "
                f"payment(s) awaiting fulfillment "
                f"(run: Run-Fulfill.ps1 -Email <addr> -Url <site>)"
            )
            lines.append(line)
            # Surface up to 5 most recent processed events so operator sees
            # exactly which emails are pending and what they bought.
            shown = 0
            for ev in payments.recent_events:
                if ev.process_status != "processed":
                    continue
                if shown >= 5:
                    break
                amt = f"${ev.amount_total_usd:.2f}" if ev.amount_total_usd else "?"
                offer = ev.hf_offer_code or "?"
                # If the customer supplied a URL via custom_fields but the
                # offer wasn't whitelisted (or auto-fulfill failed), include
                # it so the operator can copy-paste straight into Run-Fulfill.
                url_hint = f", url={ev.customer_website_url}" if ev.customer_website_url else ""
                lines.append(
                    dim(
                        f"     - {ev.customer_email}  "
                        f"({amt} {ev.currency.upper()}, offer={offer}{url_hint})"
                    )
                )
                shown += 1
    else:
        lines.append(dim("  Recent payments: (no webhook events yet)"))
    lines.append("")

    # -- -------- 4b2. INTAKE FUNNEL HEALTH ---------------------------------
    # The site Samus owns is a major sales funnel; a silent break here looks
    # identical to "nobody's interested." Three signals: schema loads, leads
    # arriving, checkout starts vs completions. Failure modes we want to catch
    # BEFORE learning about them from a customer email:
    #   * form-schema module chain broke -> operator won't see it in Cloud Run
    #     dashboards
    #   * zero submissions over 48h against a non-zero historical baseline ->
    #     the form or a downstream endpoint may be 404/500
    #   * checkouts started but never completed -> stale buy link or checkout
    #     wall on the customer side; abandons prove intent even when nothing
    #     completes.
    lines.append(bold("INTAKE FUNNEL"))
    _funnel_lines: list[str] = []
    try:
        from backend.intake.form_schema import get_form_schema

        _schema = get_form_schema()
        if _schema.fields:
            _funnel_lines.append(green("  v  form schema: OK"))
        else:
            _funnel_lines.append(red("  !  form schema: EMPTY field set"))
    except Exception as _fs_exc:  # noqa: BLE001
        _funnel_lines.append(red(f"  !  form schema: FAILED to load ({_fs_exc!s})"))
    # Onboarding lead volume in the last 48h. Zero over the window against
    # any past history is the SUSPECT_FUNNEL_BROKEN case the operator asked
    # for — we can't distinguish "no interest" from "form silently broke"
    # without a positive baseline, but SURFACING the zero here is the fix:
    # it puts the anomaly in front of the operator alongside CASH so a broken
    # site can't hide behind a slow-sales day.
    try:
        from datetime import datetime, timedelta, timezone
        from backend.intake.service import list_recent_leads

        _leads_res = list_recent_leads(limit=100)
        _leads = list(getattr(_leads_res, "leads", None) or [])
        _cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        _recent = 0
        for _l in _leads:
            _created_raw = getattr(_l, "created_at", "") or ""
            try:
                _created = datetime.fromisoformat(_created_raw.replace("Z", "+00:00"))
                if _created >= _cutoff:
                    _recent += 1
            except Exception:  # noqa: BLE001
                continue
        if _recent == 0:
            _funnel_lines.append(
                red(
                    "  !  ZERO onboarding submissions in last 48h -- SUSPECT_FUNNEL_BROKEN",
                )
            )
            _funnel_lines.append(
                dim(
                    "     verify hustleforge.tech onboarding page + "
                    "POST /intake/onboarding + form-schema fetch"
                )
            )
        else:
            _funnel_lines.append(dim(f"  form submissions (48h): {_recent}"))
    except Exception as _lead_exc:  # noqa: BLE001
        _funnel_lines.append(dim(f"  form submissions: unavailable ({_lead_exc!s})"))
    # Checkout funnel signals — the recording added in webhook.py for
    # checkout.session.created (started) and checkout.session.expired
    # (abandoned). Abandons prove buyers clicked; missing abandons AGAINST
    # missing completions is the "dead buy link" signature.
    try:
        from backend.finance.webhook import load_recent_events

        _fev = load_recent_events(7)
        _started = sum(1 for e in _fev if e.event_type == "checkout.session.created")
        _expired = sum(1 for e in _fev if e.event_type == "checkout.session.expired")
        _completed = sum(
            1
            for e in _fev
            if e.event_type == "checkout.session.completed" and e.process_status == "processed"
        )
        if _started and _completed == 0:
            _funnel_lines.append(
                red(
                    f"  !  checkouts started (7d): {_started}, "
                    f"completed: 0 -- SUSPECT_CHECKOUT_BROKEN"
                )
            )
        elif _started or _expired:
            _funnel_lines.append(
                dim(
                    f"  checkouts (7d): started={_started}, "
                    f"abandoned={_expired}, completed={_completed}"
                )
            )
    except Exception:  # noqa: BLE001 — funnel signals are best-effort surface
        pass
    lines.extend(_funnel_lines)
    lines.append("")

    # -- -------- 4b1. UPSELL NURTURE (post-audit) ---------------------------
    upsell = get_upsell_summary(window_days=14)
    if upsell.log_loaded and (
        upsell.queued_count or upsell.sent_count or upsell.failed_count or upsell.converted_count
    ):
        header = (
            f"  Upsells (14d): {upsell.queued_count} queued, "
            f"{upsell.sent_count} sent, "
            f"{upsell.converted_count} converted"
        )
        lines.append(dim(header))
        if upsell.due_now_count > 0:
            lines.append(
                yellow(f"  -> {upsell.due_now_count} due now (next runner pass within the hour)")
            )
        if upsell.failed_count > 0:
            lines.append(
                red(
                    f"  !  {upsell.failed_count} upsell email(s) FAILED "
                    f"(check finance container logs)"
                )
            )
        for r in upsell.recent_sent[:3]:
            lines.append(
                dim(
                    f"     v {r.customer_email}  "
                    f"(touch {r.touch_num} {r.source_offer_code} -> {r.target_offer_code})"
                )
            )
    lines.append("")

    # -- -------- 4c. VOICE (Vapi) -- ----------------------------------------
    voice = get_voice_call_summary(window_hours=24)
    if not voice.log_loaded:
        lines.append(dim("  Voice activity (last 24h): (no voice_audit log yet)"))
    elif voice.total_calls == 0 and voice.dial_attempt_count == 0:
        lines.append(dim("  Voice activity (last 24h): 0 calls"))
    else:
        # Dialer run rollup — even before any call finishes, shows what the
        # operator (or autonomous schedule) kicked off today.
        if voice.dial_attempt_count > 0:
            dial_bits: list[str] = []
            for outcome in (
                "initiated",
                "dry_run",
                "skipped_hours",
                "skipped_phone",
                "skipped_priority",
                "vapi_error",
            ):
                n = voice.dial_attempts_by_outcome.get(outcome)
                if n:
                    color = (
                        green
                        if outcome == "initiated"
                        else (red if outcome == "vapi_error" else dim)
                    )
                    dial_bits.append(color(f"{n} {outcome}"))
            if dial_bits:
                lines.append(
                    f"  Morning dialer (last 24h): {voice.dial_attempt_count} attempts | "
                    + ", ".join(dial_bits)
                )
        if voice.total_calls > 0:
            header = (
                f"  Voice activity (last 24h): {voice.total_calls} events "
                f"({voice.initiated_count} initiated, "
                f"{voice.end_of_call_count} ended)"
            )
            lines.append(dim(header))
        if voice.by_recommended_action:
            parts: list[str] = []
            for action in ("book_call", "follow_up", "disqualify"):
                n = voice.by_recommended_action.get(action)
                if n:
                    color = (
                        green
                        if action == "book_call"
                        else (yellow if action == "follow_up" else dim)
                    )
                    parts.append(color(f"{n} {action}"))
            none_count = voice.by_recommended_action.get("(none)", 0)
            if none_count:
                parts.append(dim(f"{none_count} no_outcome"))
            if parts:
                lines.append("  " + "  ".join(parts))
        if voice.avg_intent_score is not None:
            tier_bits: list[str] = []
            for tier in ("priority", "high", "medium", "low"):
                n = voice.by_tier.get(tier)
                if n:
                    color = (
                        green
                        if tier in ("priority", "high")
                        else (yellow if tier == "medium" else dim)
                    )
                    tier_bits.append(color(f"{n} {tier}"))
            tier_str = ", ".join(tier_bits) if tier_bits else dim("none scored")
            lines.append(f"  Avg intent: {voice.avg_intent_score:.0f}/100 | Tiers: {tier_str}")
        if voice.booked_calls:
            lines.append(green("  Booked from yesterday's calls:"))
            for r in voice.booked_calls:
                intent = f"intent {r.intent_score}" if r.intent_score is not None else "intent ?"
                lines.append(dim(f"     - {r.call_id}  ({intent}, tier={r.tier or '?'})"))
        elif voice.recent_high_intent:
            lines.append(yellow("  High-intent leads (no booking yet):"))
            for r in voice.recent_high_intent[:3]:
                intent = f"intent {r.intent_score}" if r.intent_score is not None else "intent ?"
                lines.append(dim(f"     - {r.call_id}  ({intent}, tier={r.tier})"))
    lines.append("")

    # -- -------- 5. INFO GAPS ROLLUP -- -------------------------------------
    lines.append(bold(f"OPEN INFO GAPS  ({info_gaps.open_total} total)"))
    by_pri = info_gaps.open_by_priority
    parts: list[str] = []
    if by_pri.get("critical"):
        parts.append(red(f"{by_pri['critical']} critical"))
    if by_pri.get("high"):
        parts.append(yellow(f"{by_pri['high']} high"))
    if by_pri.get("medium"):
        parts.append(f"{by_pri['medium']} medium")
    if by_pri.get("low"):
        parts.append(dim(f"{by_pri['low']} low"))
    lines.append("  " + (" | ".join(parts) if parts else dim("(none)")))
    lines.append("")
    lines.append(SEP)
    return "\n".join(lines)


# -- -------------------------------------------------------------------------
# CLI entrypoint
# -- -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Samus morning briefing -- debt + cash + sales + gaps on one screen",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors (for piping to a file / log)",
    )
    args = parser.parse_args(argv)
    if args.no_color:
        os.environ["SAMUS_MORNING_NO_COLOR"] = "1"
    try:
        sys.stdout.write(render_briefing() + "\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"morning briefing failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
