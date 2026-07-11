"""Day-over-day call strategy reasoning.

Compares today's PatternReport snapshot against the previous N days to
identify trends, compute deltas, and produce a prioritised strategy brief
that tells the operator what to change tomorrow.

Pipeline stage 6.5 — runs after pattern_aggregator, before callsheet_updater.

Fully offline: strategic synthesis runs on LM Studio via local_llm.chat().
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from backend.common import storage
from backend.common.dates import iso_now
from backend.common.local_llm import chat as llm_chat

from .pattern_aggregator import (
    PatternReport,
    list_snapshots,
    load_snapshot,
)

_LOG = logging.getLogger("samus.voice.call_strategy_reasoner")

_STRATEGY_FILE = "voice/strategy_brief.json"
_LOOKBACK_DAYS = 7


@dataclass
class TrendDelta:
    metric: str
    previous: float
    current: float
    direction: str  # "up", "down", "flat"
    magnitude: float  # absolute change


@dataclass
class StrategyBrief:
    generated_ts: str
    lookback_days: int
    days_with_data: int
    trend_deltas: list[TrendDelta] = field(default_factory=list)
    new_objections: list[str] = field(default_factory=list)
    lost_objections: list[str] = field(default_factory=list)
    improving_points: list[str] = field(default_factory=list)
    degrading_points: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    synthesis: str = ""
    llm_error: str | None = None


def reason(today_report: PatternReport | None = None) -> StrategyBrief:
    """Build a strategy brief by diffing today against recent history."""
    ts = iso_now()

    if today_report is None:
        from .pattern_aggregator import load_pattern_report

        today_report = load_pattern_report()

    if today_report is None:
        return StrategyBrief(
            generated_ts=ts,
            lookback_days=0,
            days_with_data=0,
            synthesis="No pattern data available yet.",
        )

    # ── Load historical snapshots ─────────────────────────────────
    snapshot_dates = list_snapshots(limit=_LOOKBACK_DAYS + 1)
    today_str = ts[:10]

    prior_reports: list[tuple[str, PatternReport]] = []
    for d in snapshot_dates:
        if d == today_str:
            continue
        snap = load_snapshot(d)
        if snap is not None:
            prior_reports.append((d, snap))

    days_with_data = len(prior_reports) + 1  # +1 for today

    # ── Compute deltas ────────────────────────────────────────────
    trend_deltas: list[TrendDelta] = []
    new_objections: list[str] = []
    lost_objections: list[str] = []
    improving_points: list[str] = []
    degrading_points: list[str] = []

    if prior_reports:
        prev_date, prev_report = prior_reports[0]  # most recent prior day

        # Outcome rate trend
        rate_delta = today_report.positive_outcome_rate - prev_report.positive_outcome_rate
        trend_deltas.append(
            TrendDelta(
                metric="positive_outcome_rate",
                previous=prev_report.positive_outcome_rate,
                current=today_report.positive_outcome_rate,
                direction="up" if rate_delta > 0.01 else ("down" if rate_delta < -0.01 else "flat"),
                magnitude=round(abs(rate_delta), 3),
            )
        )

        # Reward trend
        reward_delta = today_report.avg_reward - prev_report.avg_reward
        trend_deltas.append(
            TrendDelta(
                metric="avg_reward",
                previous=prev_report.avg_reward,
                current=today_report.avg_reward,
                direction="up"
                if reward_delta > 0.01
                else ("down" if reward_delta < -0.01 else "flat"),
                magnitude=round(abs(reward_delta), 3),
            )
        )

        # Objection diff
        today_obj_texts = {o.text for o in today_report.top_objections}
        prev_obj_texts = {o.text for o in prev_report.top_objections}
        new_objections = sorted(today_obj_texts - prev_obj_texts)
        lost_objections = sorted(prev_obj_texts - today_obj_texts)

        # Talking point movement
        today_landed = set(today_report.talking_points_landed)
        today_flopped = set(today_report.talking_points_flopped)
        prev_landed = set(prev_report.talking_points_landed)
        prev_flopped = set(prev_report.talking_points_flopped)

        # Points that moved from flopped → landed (improving)
        improving_points = sorted(today_landed & prev_flopped)
        # Points that moved from landed → flopped (degrading)
        degrading_points = sorted(today_flopped & prev_landed)

        # Outcome distribution shift
        for outcome in set(
            list(today_report.outcome_distribution) + list(prev_report.outcome_distribution)
        ):
            t_count = today_report.outcome_distribution.get(outcome, 0)
            p_count = prev_report.outcome_distribution.get(outcome, 0)
            t_total = today_report.total_calls_analyzed or 1
            p_total = prev_report.total_calls_analyzed or 1
            t_pct = t_count / t_total
            p_pct = p_count / p_total
            delta = t_pct - p_pct
            if abs(delta) > 0.05:
                trend_deltas.append(
                    TrendDelta(
                        metric=f"outcome_pct:{outcome}",
                        previous=round(p_pct, 3),
                        current=round(t_pct, 3),
                        direction="up" if delta > 0 else "down",
                        magnitude=round(abs(delta), 3),
                    )
                )

    # ── LLM strategic synthesis ───────────────────────────────────
    synthesis = ""
    recommendations: list[str] = []
    llm_error: str | None = None

    diff_context = _build_diff_context(
        today_report,
        prior_reports,
        trend_deltas,
        new_objections,
        lost_objections,
        improving_points,
        degrading_points,
    )

    raw = llm_chat(
        system=_SYSTEM_PROMPT,
        user=diff_context,
        max_tokens=1500,
        temperature=0.2,
        timeout=120.0,
    )

    if not raw.strip():
        llm_error = "lm_studio_empty_response"
        _LOG.warning("call_strategy_reasoner: LM Studio returned empty response")
    else:
        synthesis, recommendations = _parse_llm_response(raw)

    brief = StrategyBrief(
        generated_ts=ts,
        lookback_days=_LOOKBACK_DAYS,
        days_with_data=days_with_data,
        trend_deltas=trend_deltas,
        new_objections=new_objections,
        lost_objections=lost_objections,
        improving_points=improving_points,
        degrading_points=degrading_points,
        recommendations=recommendations,
        synthesis=synthesis,
        llm_error=llm_error,
    )

    _persist(brief)
    return brief


_SYSTEM_PROMPT = """\
You are a cold-calling sales coach analyzing day-over-day call performance data.
Your job is to produce 3-5 specific, actionable recommendations for tomorrow's calls.

Rules:
- Be specific: reference actual talking points, objections, and outcomes from the data.
- Prioritize by expected impact: fixing a 0% voicemail callback rate matters more than tweaking a working opener.
- Each recommendation should be one concrete action ("Replace X with Y", "When you hear Z, respond with W").
- If positive_outcome_rate is 0% or near-zero, focus on the fundamentals: opener hook, voicemail callback rate, gatekeeper bypass.
- Note what IS working so the caller doesn't accidentally break it.

Output format:
SYNTHESIS:
<2-3 sentence overview of the trend and what it means>

RECOMMENDATIONS:
1. <specific action>
2. <specific action>
3. <specific action>
[up to 5]

KEEP DOING:
- <thing that's working>
"""


def _build_diff_context(
    today: PatternReport,
    prior: list[tuple[str, PatternReport]],
    deltas: list[TrendDelta],
    new_obj: list[str],
    lost_obj: list[str],
    improving: list[str],
    degrading: list[str],
) -> str:
    lines = [
        f"TODAY ({today.generated_ts[:10]}): {today.total_calls_analyzed} calls, "
        f"{today.positive_outcome_rate:.1%} positive, avg reward {today.avg_reward:.3f}",
        f"Outcomes: {json.dumps(today.outcome_distribution)}",
    ]

    if prior:
        prev_date, prev = prior[0]
        lines.append(
            f"\nPREVIOUS DAY ({prev_date}): {prev.total_calls_analyzed} calls, "
            f"{prev.positive_outcome_rate:.1%} positive, avg reward {prev.avg_reward:.3f}"
        )
        lines.append(f"Outcomes: {json.dumps(prev.outcome_distribution)}")

    if len(prior) > 1:
        rates = [f"{d}: {r.positive_outcome_rate:.1%}" for d, r in prior[:5]]
        lines.append(f"\nHISTORICAL RATES: {', '.join(rates)}")

    if deltas:
        lines.append("\nTREND DELTAS:")
        for d in deltas:
            lines.append(
                f"  {d.metric}: {d.previous:.3f} → {d.current:.3f} ({d.direction}, Δ{d.magnitude:.3f})"
            )

    if new_obj:
        lines.append(f"\nNEW OBJECTIONS (not seen before): {json.dumps(new_obj)}")
    if lost_obj:
        lines.append(f"\nRESOLVED OBJECTIONS (no longer appearing): {json.dumps(lost_obj)}")

    if improving:
        lines.append(
            f"\nIMPROVING TALKING POINTS (were flopping, now landing): {json.dumps(improving[:5])}"
        )
    if degrading:
        lines.append(
            f"\nDEGRADING TALKING POINTS (were landing, now flopping): {json.dumps(degrading[:5])}"
        )

    # Top objections with effectiveness
    if today.top_objections:
        lines.append("\nCURRENT OBJECTIONS:")
        for o in today.top_objections[:8]:
            lines.append(
                f'  "{o.text}" — {o.count}x, {o.avg_handling_effectiveness:.0%} effective, '
                f'best handler: "{o.best_handler}"'
            )

    # Script feedback
    if today.script_feedback_summary:
        lines.append("\nSCRIPT FEEDBACK (from LLM analysis of each call):")
        for element, feedback in today.script_feedback_summary.items():
            lines.append(f"  {element}: {feedback[:300]}")

    # Talking points
    if today.talking_points_landed:
        lines.append(f"\nLANDED: {json.dumps(today.talking_points_landed[:8])}")
    if today.talking_points_flopped:
        lines.append(f"\nFLOPPED: {json.dumps(today.talking_points_flopped[:8])}")

    return "\n".join(lines)


def _parse_llm_response(raw: str) -> tuple[str, list[str]]:
    """Extract synthesis and recommendations from the LLM output."""
    synthesis = ""
    recommendations: list[str] = []

    sections = raw.split("RECOMMENDATIONS:")
    if len(sections) >= 2:
        pre = sections[0]
        post = sections[1]

        # Extract synthesis
        if "SYNTHESIS:" in pre:
            synthesis = pre.split("SYNTHESIS:", 1)[1].strip()
        else:
            synthesis = pre.strip()

        # Extract recommendations (numbered lines)
        rec_section = post.split("KEEP DOING:")[0] if "KEEP DOING:" in post else post
        for line in rec_section.strip().splitlines():
            line = line.strip()
            if line and line[0].isdigit() and "." in line[:4]:
                rec_text = line.split(".", 1)[1].strip() if "." in line[:4] else line
                if rec_text:
                    recommendations.append(rec_text)

        # Append keep-doing items as context
        if "KEEP DOING:" in post:
            keep_section = post.split("KEEP DOING:", 1)[1].strip()
            for line in keep_section.splitlines():
                line = line.strip().lstrip("- ").strip()
                if line:
                    recommendations.append(f"[KEEP] {line}")
    else:
        synthesis = raw.strip()[:500]

    return synthesis, recommendations


def _persist(brief: StrategyBrief) -> None:
    try:
        target = storage.root() / _STRATEGY_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(brief), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        _LOG.info(
            "call_strategy_reasoner: wrote brief — %d days data, %d recommendations",
            brief.days_with_data,
            len(brief.recommendations),
        )
    except OSError as exc:
        _LOG.warning("strategy brief persist failed: %s", exc)


def load_strategy_brief() -> StrategyBrief | None:
    """Load the last persisted strategy brief."""
    target = storage.root() / _STRATEGY_FILE
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        deltas_raw = data.pop("trend_deltas", []) or []
        deltas = [TrendDelta(**d) for d in deltas_raw if isinstance(d, dict)]
        brief = StrategyBrief(**data)
        brief.trend_deltas = deltas
        return brief
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("load_strategy_brief failed: %s", exc)
        return None
