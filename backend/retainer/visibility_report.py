"""Monthly visibility report renderer for retainer customers.

Pure rendering — no I/O beyond loading prior-month and this-month audit
snapshots from the customer artifact directory. The monthly_cycle DAG
calls this in its ``report.render_and_send`` step; the result is written
under ``customers/<slug>/<sku>/<YYYY-MM>/visibility_report.md`` and then
emailed by the cycle runner.

Style matches ``backend/seo/report.py`` — ASCII only, no em-dashes, no
unicode bullets, so the report renders cleanly in Windows console + plain
text email clients alongside the rich HTML mirror.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Diff / movement primitives
# ---------------------------------------------------------------------------


def _arrow(delta: int | float) -> str:
    """ASCII arrow: ``+5`` -> ``->`` (up), ``-3`` -> ``<-`` (down), ``0`` -> ``--``."""
    if delta > 0:
        return "UP"
    if delta < 0:
        return "DOWN"
    return "FLAT"


def _format_delta(delta: int | float, unit: str = "") -> str:
    """Render a signed delta with arrow + magnitude. ``+18.0%`` style."""
    if isinstance(delta, float) and not delta.is_integer():
        body = f"{delta:+.1f}{unit}"
    else:
        body = f"{int(delta):+d}{unit}"
    return f"{body} ({_arrow(delta)})"


def _rank_movement(
    this_month_ranks: dict[str, int],
    prior_month_ranks: dict[str, int],
) -> list[dict[str, Any]]:
    """Per-keyword rank diff between two monthly snapshots.

    Lower rank number = better Google position; a move from #7 to #4 is a
    +3 improvement, so we invert the raw subtraction so positive deltas
    always mean "better". New keywords (not in prior_month) get
    ``delta=None`` and ``status="new"``.
    """
    out: list[dict[str, Any]] = []
    for keyword, this_rank in this_month_ranks.items():
        prior_rank = prior_month_ranks.get(keyword)
        if prior_rank is None:
            out.append(
                {
                    "keyword": keyword,
                    "this_rank": this_rank,
                    "prior_rank": None,
                    "delta": None,
                    "status": "new",
                }
            )
        else:
            # Positive delta == improvement (smaller rank number wins)
            delta = prior_rank - this_rank
            out.append(
                {
                    "keyword": keyword,
                    "this_rank": this_rank,
                    "prior_rank": prior_rank,
                    "delta": delta,
                    "status": "tracked",
                }
            )
    # Lost keywords (tracked last month, gone this month)
    for keyword, prior_rank in prior_month_ranks.items():
        if keyword not in this_month_ranks:
            out.append(
                {
                    "keyword": keyword,
                    "this_rank": None,
                    "prior_rank": prior_rank,
                    "delta": None,
                    "status": "dropped",
                }
            )
    return out


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_visibility_report(
    *,
    customer_id: str,
    sku_id: str,
    this_month: dict[str, Any],
    prior_month: dict[str, Any] | None = None,
    fixes_applied: list[dict[str, str]] | None = None,
    next_month_focus: list[str] | None = None,
    customer_name: str = "",
    site_url: str = "",
    ts: str | None = None,
) -> str:
    """Render the monthly visibility report as markdown.

    Args:
      customer_id:      slug; appears in the closing footer for traceability
      sku_id:           ``retainer_seo_optimization`` or any
                        ``retainer_ai_ops_partner_*`` tier — drives the
                        section ordering + section names
      this_month:       audit snapshot dict: keys ``seo_score`` (int),
                        ``rank_by_keyword`` (dict[str, int]),
                        ``gsc_clicks`` (int), ``gsc_impressions`` (int),
                        ``issue_count`` (int)
      prior_month:      same shape as this_month, or None for the first
                        month (renders "first cycle — baseline" copy)
      fixes_applied:    list of {"area": str, "description": str} dicts;
                        defaults to a sensible placeholder if not supplied
      next_month_focus: bullet list of priorities for next cycle
      customer_name:    optional greeting personalization
      site_url:         the audited site (rendered in the header)
      ts:               override the report timestamp for tests

    Returns:
      The full markdown body (no I/O — caller writes the file).
    """
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    month_label = ts[:7]  # YYYY-MM
    is_first_cycle = prior_month is None
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"

    # AI Ops Partner uses a different report skeleton (no rank table).
    # Matches all three tiers: _starter, _growth, _scale.
    if sku_id.startswith("retainer_ai_ops_partner"):
        return _render_ai_ops_report(
            month_label=month_label,
            customer_id=customer_id,
            customer_name=customer_name,
            this_month=this_month,
            prior_month=prior_month,
            fixes_applied=fixes_applied or [],
            next_month_focus=next_month_focus or [],
            ts=ts,
        )

    # SEO Optimization (default)
    lines: list[str] = []
    lines.append(f"# SEO Visibility Report — {month_label}")
    lines.append("")
    if site_url:
        lines.append(f"**Site:** {site_url}")
    lines.append(f"**Cycle:** {month_label}")
    lines.append(f"**Generated:** {ts}")
    lines.append("")
    lines.append(greeting)
    lines.append("")
    if is_first_cycle:
        lines.append(
            "This is your first monthly cycle, so this report is the "
            "baseline we'll measure future months against. Starting next "
            "month, the rank-movement table will show month-over-month "
            "deltas + the GSC delta will be a real comparison."
        )
    else:
        lines.append("Here's what changed on your site this month and what we did about it.")
    lines.append("")

    # ----- Section 1: Headline metrics -----
    lines.append("## Headline metrics")
    lines.append("")
    seo_score = this_month.get("seo_score", 0)
    if not is_first_cycle:
        prior_score = (prior_month or {}).get("seo_score", 0)
        score_delta = seo_score - prior_score
        lines.append(
            f"- **SEO score:** {seo_score}/100  "
            f"(prior: {prior_score}/100, {_format_delta(score_delta)})"
        )
    else:
        lines.append(f"- **SEO score:** {seo_score}/100  (baseline)")

    this_clicks = this_month.get("gsc_clicks", 0)
    this_impr = this_month.get("gsc_impressions", 0)
    if not is_first_cycle:
        prior_clicks = (prior_month or {}).get("gsc_clicks", 0)
        prior_impr = (prior_month or {}).get("gsc_impressions", 0)
        clicks_pct = ((this_clicks - prior_clicks) / max(prior_clicks, 1)) * 100
        impr_pct = ((this_impr - prior_impr) / max(prior_impr, 1)) * 100
        lines.append(
            f"- **Google Search Console clicks:** {this_clicks:,}  "
            f"(prior: {prior_clicks:,}, {_format_delta(clicks_pct, '%')})"
        )
        lines.append(
            f"- **GSC impressions:** {this_impr:,}  "
            f"(prior: {prior_impr:,}, {_format_delta(impr_pct, '%')})"
        )
    else:
        lines.append(f"- **GSC clicks:** {this_clicks:,}  (baseline)")
        lines.append(f"- **GSC impressions:** {this_impr:,}  (baseline)")

    issue_count = this_month.get("issue_count", 0)
    lines.append(f"- **Open issues at start of cycle:** {issue_count}")
    lines.append("")

    # ----- Section 2: Rank movement -----
    lines.append("## Rank movement")
    lines.append("")
    this_ranks = this_month.get("rank_by_keyword") or {}
    prior_ranks = (prior_month or {}).get("rank_by_keyword") or {}
    if not this_ranks:
        lines.append("_No tracked keywords for this cycle._")
    else:
        movements = _rank_movement(this_ranks, prior_ranks)
        lines.append("| Keyword | This month | Prior month | Movement |")
        lines.append("|---|---|---|---|")
        for m in movements:
            kw = m["keyword"]
            this_r = m["this_rank"] if m["this_rank"] is not None else "—"
            prior_r = m["prior_rank"] if m["prior_rank"] is not None else "—"
            if m["status"] == "new":
                move = "new this month"
            elif m["status"] == "dropped":
                move = "dropped from tracking"
            else:
                move = _format_delta(m["delta"], " positions")
            lines.append(f"| {kw} | #{this_r} | #{prior_r} | {move} |")
    lines.append("")

    # ----- Section 3: Fixes applied -----
    lines.append("## Fixes applied this month")
    lines.append("")
    if not fixes_applied:
        # First-cycle baseline often has nothing yet; flag that explicitly
        # rather than leaving the section empty.
        if is_first_cycle:
            lines.append(
                "_No fixes applied this cycle — Month 1 establishes the "
                "baseline. The fix queue is now populated for Month 2._"
            )
        else:
            lines.append("_No fixes applied this cycle._")
    else:
        for fix in fixes_applied:
            area = fix.get("area", "general")
            desc = fix.get("description", "(no detail)")
            lines.append(f"- **{area}:** {desc}")
    lines.append("")

    # ----- Section 4: Next month focus -----
    lines.append("## Next month's focus")
    lines.append("")
    if not next_month_focus:
        # Sensible default surface if the cycle didn't supply priorities
        next_month_focus = [
            "continue rank improvement on top-3 keywords",
            "address any new Core Web Vitals regressions",
            "expand internal linking to underperforming pages",
        ]
    for item in next_month_focus:
        lines.append(f"- {item}")
    lines.append("")

    # ----- Footer -----
    lines.append("---")
    lines.append("")
    lines.append(
        "If you'd like to flag a specific page, keyword, or competitor to "
        "prioritize next cycle, just reply to this email."
    )
    lines.append("")
    lines.append("— Morgan, HustleForge")
    lines.append("")
    lines.append(f"_Customer: {customer_id} | Cycle: {month_label} | Report: visibility_v1_")
    return "\n".join(lines)


def _render_ai_ops_report(
    *,
    month_label: str,
    customer_id: str,
    customer_name: str,
    this_month: dict[str, Any],
    prior_month: dict[str, Any] | None,
    fixes_applied: list[dict[str, str]],
    next_month_focus: list[str],
    ts: str,
) -> str:
    """AI Ops Partner monthly report — ops shipped + metrics saved."""
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    lines: list[str] = []
    lines.append(f"# AI Ops Partner — Monthly Report ({month_label})")
    lines.append("")
    lines.append(f"**Generated:** {ts}")
    lines.append("")
    lines.append(greeting)
    lines.append("")
    lines.append(
        "Here's what shipped this month, what it's saving you, and what's "
        "queued for the next cycle."
    )
    lines.append("")

    # Metrics block (free-form — AI Ops doesn't have a fixed schema like SEO)
    lines.append("## What we shipped this month")
    lines.append("")
    if not fixes_applied:
        lines.append("_No deliverables shipped this cycle — see the operator notes section below._")
    else:
        for f in fixes_applied:
            area = f.get("area", "automation")
            desc = f.get("description", "(no detail)")
            lines.append(f"- **{area}:** {desc}")
    lines.append("")

    # Metrics snapshot
    lines.append("## Operational metrics")
    lines.append("")
    metrics = this_month.get("ops_metrics") or {}
    if not metrics:
        lines.append("_Metrics not captured this cycle._")
    else:
        for key, val in metrics.items():
            lines.append(f"- **{key}:** {val}")
    lines.append("")

    # Next month
    lines.append("## Queued for next cycle")
    lines.append("")
    if not next_month_focus:
        next_month_focus = [
            "review backlog with operator on Week 1 check-in",
            "scope next-month build in Week 2 prioritization",
        ]
    for item in next_month_focus:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Calendar invite for next cycle's Week 1 assessment call is in your inbox separately."
    )
    lines.append("")
    lines.append("— Morgan, HustleForge")
    lines.append("")
    lines.append(f"_Customer: {customer_id} | Cycle: {month_label} | Report: ai_ops_v1_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Snapshot read / write helpers (used by monthly_cycle steps)
# ---------------------------------------------------------------------------


def write_snapshot(
    customer_id: str,
    sku_id: str,
    cycle_month: str,  # YYYY-MM
    snapshot: dict[str, Any],
) -> Path:
    """Persist this-month's audit snapshot for next-month's diff."""
    from backend.common import storage

    target_dir = storage.root() / "customers" / customer_id / sku_id / cycle_month
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "snapshot.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return path


def read_snapshot(
    customer_id: str,
    sku_id: str,
    cycle_month: str,  # YYYY-MM
) -> dict[str, Any] | None:
    """Load a prior cycle's snapshot, or None if no file exists yet."""
    from backend.common import storage

    path = storage.root() / "customers" / customer_id / sku_id / cycle_month / "snapshot.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


__all__ = [
    "render_visibility_report",
    "write_snapshot",
    "read_snapshot",
]
