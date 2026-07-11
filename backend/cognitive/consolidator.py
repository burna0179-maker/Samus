"""Nightly memory consolidation — DISTILL / PROMOTE / CALIBRATE / COMPRESS.

Tranche 3 (learning loop). Runs once nightly (in-container timer in
:mod:`backend.cognitive.consolidation_task`, host Task Scheduler fallback via
``scripts/Register-ConsolidationSchedule.ps1``) and turns the day's raw
operational exhaust into durable memory:

  a. **DISTILL** — scan the day's reward ledger + conversion funnel +
     experiment arm stats + outreach angle performance -> semantic lessons
     (pattern strings with supporting stats + provenance refs) written to the
     guidance ledger via the existing :func:`ingest_guidance` path and
     auto-ACCEPTED so they flow into ``active_guidance_context()`` (the
     REASON-stage seam). Deterministic heuristics first; ONE optional metered
     LLM call may rephrase the lessons (template fallback).
  b. **PROMOTE** — :func:`backend.experiments.promoter.run_nightly_promotion`.
  c. **CALIBRATE** — recompute CRM tier close-probabilities + optimizer seed
     constants from actual closed-loop funnel/reward rates; written to
     :mod:`backend.common.calibration` (override-if-present store — the
     scoring/optimizer read-side wiring lands at merge; see calibration.py).
  d. **COMPRESS** — age-rotate the busiest JSONL ledgers via the existing
     ``JsonlLedger.rotate_by_age`` and supersession-compact the guidance
     ledger.
  e. **EXPIRE_GUIDANCE** — env-gated (``SAMUS_GUIDANCE_STALE_DAYS``, default
     OFF) triage-drain: abandon PROPOSED guidance recs that were never triaged
     within N days. ``abandon()`` is terminal and touches NO effector, so this
     drains the backlog of externally-sourced recommendations (day-start /
     EOD / CODB-reasoner / gameplan) that have no other automated triage path —
     without ACTING on any of them. It NEVER auto-accepts (that would violate
     the guidance module's wire-not-arm contract).

Every stage is fail-soft and individually callable; ``run_consolidation``
never raises. Distilled lessons carry ``source_question="distilled"`` and a
``distilled-<day>`` briefing id — the "tier=distilled" provenance marker.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.common.business_events_shim_t3 import read_events
from backend.common.dates import business_date, business_today, iso_now
from backend.common.state_paths import state_path

log = logging.getLogger("samus.cognitive.consolidator")

ENV_RETENTION_DAYS = "SAMUS_CONSOLIDATION_RETENTION_DAYS"
ENV_LLM_ENABLED = "SAMUS_CONSOLIDATION_LLM_ENABLED"
ENV_CALIBRATE_MIN_SAMPLE = "SAMUS_CALIBRATION_MIN_OPPORTUNITIES"
ENV_GUIDANCE_STALE_DAYS = "SAMUS_GUIDANCE_STALE_DAYS"

DEFAULT_RETENTION_DAYS = 180
DEFAULT_CALIBRATE_MIN_SAMPLE = 20
_TRAILING_DAYS = 7
_LLM_WORKCELL = "cognitive"
_LLM_MAX_TOKENS = 600

# Calibration scale factor is clamped so one weird day can't nuke or inflate
# the tier baselines beyond reason.
_CALIBRATION_FACTOR_MIN = 0.25
_CALIBRATION_FACTOR_MAX = 4.0


def _llm_enabled() -> bool:
    raw = (os.getenv(ENV_LLM_ENABLED) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _retention_days() -> float:
    raw = (os.getenv(ENV_RETENTION_DAYS) or "").strip()
    try:
        return float(raw) if raw else DEFAULT_RETENTION_DAYS
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def _today() -> str:
    """The current business (US Pacific) day — NOT the UTC/host-local day.

    Consolidation windows ledger rows (all stamped in UTC) by business day; the
    anchor must be the SAME Pacific day the rows are bucketed into, or the
    trailing window's upper bound sits behind any row created during the PT
    evening (whose UTC date has already rolled to tomorrow) and silently drops
    it. See :func:`backend.common.dates.business_today`.
    """
    return business_today()


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Concrete-ledger readers (the sources that exist TODAY — the unified event
# stream is additive context only, via the shim).
# ---------------------------------------------------------------------------
def _reward_rows() -> list[dict[str, Any]]:
    """All rows of the ADR-004 reward audit ledger. Fail-open to []."""
    from backend.strategy import reward_density

    path = Path(reward_density._persist_path())
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log.warning("reward ledger read failed: %s", exc)
    return out


def _funnel_rows() -> list[dict[str, Any]]:
    from backend.common.conversion_funnel import _ledger

    try:
        return _ledger().tail(limit=50_000)
    except Exception as exc:  # noqa: BLE001
        log.warning("funnel ledger read failed: %s", exc)
        return []


def _window(rows: list[dict[str, Any]], *, ts_field: str, day: str,
            trailing_days: int = 0) -> list[dict[str, Any]]:
    """Rows whose BUSINESS day == day, or within trailing_days before it.

    ``day`` is a Pacific business day (see :func:`_today`); each row's UTC
    timestamp is converted to its Pacific business day before comparison, so a
    row created in the PT evening (UTC already tomorrow) still buckets into the
    day the operator is consolidating — not the one after it.
    """
    try:
        anchor = date.fromisoformat(day)
    except ValueError:
        return []
    lo = anchor - timedelta(days=trailing_days)
    out = []
    for row in rows:
        dt = _parse_ts(row.get(ts_field))
        if dt is None:
            continue
        d = date.fromisoformat(business_date(dt))
        if lo <= d <= anchor:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Stage a — DISTILL
# ---------------------------------------------------------------------------
def _deterministic_lessons(day: str) -> list[dict[str, Any]]:
    """Pattern lessons from the concrete ledgers (stats + provenance refs)."""
    lessons: list[dict[str, Any]] = []

    # --- funnel: per-industry closed_won concentration (day vs trailing 7d)
    funnel = _funnel_rows()
    week = _window(funnel, ts_field="ts", day=day, trailing_days=_TRAILING_DAYS)
    won = [r for r in week if r.get("stage") == "closed_won"]
    opps = [r for r in week if r.get("stage") == "opportunity"]
    if won:
        by_industry: dict[str, int] = {}
        for r in won:
            ind = str(r.get("industry") or "").strip()
            if ind:
                by_industry[ind] = by_industry.get(ind, 0) + 1
        if by_industry:
            top, top_n = max(by_industry.items(), key=lambda kv: kv[1])
            share = top_n / len(won)
            if share >= 0.4 and top_n >= 2:
                lessons.append({
                    "recommendation": (
                        f"pattern: '{top}' industry produced {top_n}/{len(won)} "
                        f"closed_won deals in the trailing {_TRAILING_DAYS}d "
                        f"({share:.0%}) — prioritize {top} prospects"
                    ),
                    "rationale": (
                        f"provenance: conversion_funnel ledger, window "
                        f"{_TRAILING_DAYS}d ending {day}; wins_by_industry={by_industry}"
                    ),
                    "category": "revenue_acceleration",
                    "expected_impact": "high",
                    "risk_level": "low",
                    "source_question": "distilled",
                })
    if opps:
        rate = len(won) / len(opps)
        lessons.append({
            "recommendation": (
                f"pattern: opportunity->closed_won conversion ran {rate:.1%} "
                f"({len(won)}/{len(opps)}) over the trailing {_TRAILING_DAYS}d ending {day}"
            ),
            "rationale": (
                f"provenance: conversion_funnel ledger, window {_TRAILING_DAYS}d ending {day}"
            ),
            "category": "operational_optimization",
            "expected_impact": "medium",
            "risk_level": "low",
            "source_question": "distilled",
        })

    # --- reward ledger: day's reward mass + paid terminals
    rewards = _window(_reward_rows(), ts_field="computed_at", day=day)
    if rewards:
        total = sum(float(r.get("reward", 0.0) or 0.0) for r in rewards)
        paid = sum(
            1 for r in rewards
            if float((r.get("components") or {}).get("terminal_paid", 0.0) or 0.0) > 0
        )
        lessons.append({
            "recommendation": (
                f"pattern: {len(rewards)} reward computations on {day} "
                f"(total reward {total:.1f}, {paid} payment-confirmed terminals)"
            ),
            "rationale": "provenance: strategy reward ledger (ADR-004 audit JSONL)",
            "category": "revenue_acceleration",
            "expected_impact": "medium" if paid else "low",
            "risk_level": "low",
            "source_question": "distilled",
        })

    # --- outreach angles: best win-rate angle
    try:
        from backend.outreach.metrics import get_angle_performance, snapshot

        perf = get_angle_performance()
        if perf:
            best = max(perf, key=lambda k: perf[k])
            angles = snapshot().get("angles", {})
            rec = angles.get(best, {})
            trials = int(rec.get("wins", 0)) + int(rec.get("losses", 0))
            if trials >= 3:
                lessons.append({
                    "recommendation": (
                        f"pattern: outreach angle '{best}' leads with "
                        f"{perf[best]:.0%} win rate over {trials} interactions — "
                        f"bias new callsheets toward it"
                    ),
                    "rationale": (
                        f"provenance: outreach interaction ledger / feedback store; "
                        f"angle_performance={ {k: round(v, 3) for k, v in perf.items()} }"
                    ),
                    "category": "revenue_acceleration",
                    "expected_impact": "medium",
                    "risk_level": "low",
                    "source_question": "distilled",
                })
    except Exception as exc:  # noqa: BLE001
        log.warning("distill: angle read failed: %s", exc)

    # --- experiments: current best arm per active experiment
    try:
        from backend.experiments import registry as exp_registry

        for exp in exp_registry.list_experiments(status="active"):
            stats = exp_registry.arm_stats(exp.experiment_id)
            tried = {a: s for a, s in stats.items()
                     if not s.get("archived") and s.get("trials", 0) >= 5}
            if len(tried) >= 2:
                best = max(tried, key=lambda a: tried[a]["mean_reward"])
                s = tried[best]
                lessons.append({
                    "recommendation": (
                        f"pattern: experiment '{exp.experiment_id}' "
                        f"({exp.dimension}) currently favors arm '{best}' "
                        f"(mean reward {s['mean_reward']:.3f} over {s['trials']} trials)"
                    ),
                    "rationale": (
                        f"provenance: attribution variant store, arm_id={s['arm_id']}"
                    ),
                    "category": "capability_expansion",
                    "expected_impact": "medium",
                    "risk_level": "low",
                    "source_question": "distilled",
                })
    except Exception as exc:  # noqa: BLE001
        log.warning("distill: experiment read failed: %s", exc)

    # --- unified stream (additive context, no-op pre-merge)
    try:
        paid_events = [
            e for e in read_events(event_types=["payment.received"], limit=500)
            if business_date(_parse_ts(e.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)) == day
        ]
        if paid_events:
            revenue = sum(float(e.get("revenue_usd") or 0.0) for e in paid_events)
            lessons.append({
                "recommendation": (
                    f"pattern: {len(paid_events)} payment.received events on {day} "
                    f"(${revenue:,.2f}) in the unified stream"
                ),
                "rationale": "provenance: unified business-event stream",
                "category": "revenue_acceleration",
                "expected_impact": "high",
                "risk_level": "low",
                "source_question": "distilled",
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("distill: unified-stream read failed: %s", exc)

    return lessons


def _maybe_rephrase(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ONE optional metered LLM call to phrase the lessons; template fallback.

    Only the ``recommendation`` wording may be replaced — stats + provenance
    (``rationale``) always stay the deterministic originals.
    """
    if not lessons or not _llm_enabled():
        return lessons
    try:
        from backend.common.llm_client import anthropic_messages

        numbered = "\n".join(
            f"{i + 1}. {les['recommendation']}" for i, les in enumerate(lessons)
        )
        prompt = (
            "Rephrase each numbered operational lesson below as one crisp, "
            "actionable sentence (keep every number/statistic verbatim). "
            "Reply with a JSON array of strings, one per lesson, same order.\n\n"
            + numbered
        )
        text, _usage = anthropic_messages(
            workcell=_LLM_WORKCELL, api_key="unused",
            prompt=prompt, max_tokens=_LLM_MAX_TOKENS,
        )
        start, end = text.find("["), text.rfind("]")
        phrased = json.loads(text[start:end + 1]) if start != -1 and end != -1 else []
        if isinstance(phrased, list) and len(phrased) == len(lessons):
            for les, wording in zip(lessons, phrased):
                if isinstance(wording, str) and wording.strip():
                    les["recommendation"] = wording.strip()
    except Exception as exc:  # noqa: BLE001 — budget denial / transport / parse
        log.info("distill LLM phrasing skipped (%s); deterministic wording kept", exc)
    return lessons


def distill(day: str) -> dict[str, Any]:
    """DISTILL stage: lessons -> guidance ledger (tier=distilled, auto-accepted).

    Also rolls up each distilled lesson as a structured
    :class:`backend.experiments.promoter.GuidanceLaw` in
    ``compression/guidance_laws.jsonl`` — the "wisdom over knowledge" surface
    downstream readers query as durable business law rather than raw exhaust
    (Concept 2, Samus_Assimilation_Plan_Institutional_Cognition_2026-07-06.md).
    """
    from backend.cognitive.guidance import GuidanceLedger, ingest_guidance

    lessons = _maybe_rephrase(_deterministic_lessons(day))
    if not lessons:
        return {"lessons": 0, "ingested": 0}
    led = GuidanceLedger()
    records = ingest_guidance(f"distilled-{day}", lessons, ledger=led)
    accepted = 0
    for rec in records:
        try:
            if led.accept(rec.recommendation_id) is not None:
                accepted += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("distill: accept failed %s: %s", rec.recommendation_id, exc)
    laws_emitted = _emit_guidance_laws(day, lessons, records)
    return {
        "lessons": len(lessons),
        "ingested": len(records),
        "accepted": accepted,
        "laws_emitted": laws_emitted,
    }


def _emit_guidance_laws(
    day: str,
    lessons: list[dict[str, Any]],
    records: list[Any],
) -> int:
    """Materialize freshly-accepted lessons as :class:`GuidanceLaw` rows.

    Fail-soft: any per-lesson error is logged and skipped; a rollup failure
    never sinks distill. Returns the count of rows written.
    """
    try:
        from backend.experiments.promoter import GuidanceLaw, emit_guidance_law
    except Exception as exc:  # noqa: BLE001
        log.warning("distill: guidance-law import failed: %s", exc)
        return 0

    promoted_at = iso_now()
    written = 0
    for idx, lesson in enumerate(lessons):
        try:
            law_text = str(lesson.get("recommendation") or "").strip()
            if not law_text:
                continue
            rationale = str(lesson.get("rationale") or "")
            evidence_count, confidence = _evidence_and_confidence(lesson, rationale)
            source_id = ""
            if idx < len(records):
                source_id = str(getattr(records[idx], "recommendation_id", "") or "")
            law = GuidanceLaw(
                law_id=f"law-{day}-{idx:02d}",
                law=law_text,
                evidence_count=evidence_count,
                confidence=confidence,
                promoted_at=promoted_at,
                source_pattern_ids=[s for s in (source_id, f"distilled-{day}") if s],
                category=str(lesson.get("category") or ""),
            )
            if emit_guidance_law(law):
                written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("distill: guidance-law emit skipped for lesson %d: %s", idx, exc)
            continue
    return written


def _evidence_and_confidence(
    lesson: dict[str, Any], rationale: str,
) -> tuple[int, float]:
    """Best-effort numeric support for a lesson.

    Deterministic lessons carry counts in either the ``recommendation`` text
    ("60/100 closed_won") or the ``rationale`` provenance dict. We parse the
    first ``a/b`` fraction we see; if none, evidence_count degrades to 1 and
    confidence to a floor. Never raises.
    """
    import re

    text = f"{lesson.get('recommendation') or ''} {rationale}"
    match = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if match:
        num = int(match.group(1))
        den = int(match.group(2))
        if den > 0:
            # Laplace-smoothed rate — matches belief_ledger's confidence model.
            confidence = (num + 1) / (den + 2)
            return den, round(confidence, 4)
    # Fallback: single trailing count "over 42 interactions" / "42 trials"
    single = re.search(r"(?:over\s+|of\s+)?(\d+)\s+(?:trials|interactions|events)",
                       text, re.IGNORECASE)
    if single:
        n = int(single.group(1))
        if n > 0:
            return n, 0.5
    impact = str(lesson.get("expected_impact") or "").lower()
    floor = {"high": 0.7, "medium": 0.5, "low": 0.3}.get(impact, 0.4)
    return 1, floor


# ---------------------------------------------------------------------------
# Stage b — PROMOTE
# ---------------------------------------------------------------------------
def promote() -> dict[str, Any]:
    from backend.experiments.promoter import run_nightly_promotion

    return run_nightly_promotion()


# ---------------------------------------------------------------------------
# Stage c — CALIBRATE
# ---------------------------------------------------------------------------
def calibrate(day: str) -> dict[str, Any]:
    """Recompute tier close-probabilities + optimizer seeds from actual rates.

    Uses the trailing-window opportunity->closed_won conversion as the
    observed anchor and scales the four scoring-tier baselines by
    ``observed / warm-baseline`` (clamped). Writes nothing when the sample is
    too small — the override store stays absent and legacy constants hold.
    """
    from backend.common.calibration import write_calibration
    from backend.crm.scoring import tier_close_probability

    min_sample = int(float(os.getenv(ENV_CALIBRATE_MIN_SAMPLE, "") or DEFAULT_CALIBRATE_MIN_SAMPLE))
    funnel = _window(
        _funnel_rows(), ts_field="ts", day=day, trailing_days=_TRAILING_DAYS,
    )
    opps = sum(1 for r in funnel if r.get("stage") == "opportunity")
    won = sum(1 for r in funnel if r.get("stage") == "closed_won")
    rewards = _window(_reward_rows(), ts_field="computed_at", day=day,
                      trailing_days=_TRAILING_DAYS)
    samples = {
        "window_days": _TRAILING_DAYS, "opportunities": opps,
        "closed_won": won, "reward_computations": len(rewards),
    }
    if opps < min_sample:
        return {"written": False, "reason": f"sample {opps} < {min_sample}", **samples}

    observed = won / opps
    baseline = tier_close_probability("warm")
    factor = observed / baseline if baseline > 0 else 1.0
    factor = max(_CALIBRATION_FACTOR_MIN, min(_CALIBRATION_FACTOR_MAX, factor))
    tiers = {
        tier: round(min(1.0, tier_close_probability(tier) * factor), 4)  # type: ignore[arg-type]
        for tier in ("low", "warm", "hot", "priority")
    }
    seeds = {"conversion_prob_default": round(observed, 4)}
    ok = write_calibration(
        tier_close_probability=tiers, optimizer_seeds=seeds,
        samples=samples, day=day,
    )
    return {"written": ok, "factor": round(factor, 4),
            "tier_close_probability": tiers, "optimizer_seeds": seeds, **samples}


# ---------------------------------------------------------------------------
# Stage d — COMPRESS / ARCHIVE
# ---------------------------------------------------------------------------
def compress() -> dict[str, Any]:
    """Age-rotate the busiest JSONL ledgers + compact the guidance ledger.

    Uses the existing :meth:`JsonlLedger.rotate_by_age` (archive-then-replace,
    crash-superset-safe). Retention via ``SAMUS_CONSOLIDATION_RETENTION_DAYS``
    (default 180d). Guidance uses supersession compaction, not age (a
    still-open recommendation can be arbitrarily old).
    """
    from backend.common.conversion_funnel import _ledger_path as funnel_path
    from backend.common.persistence import JsonlLedger
    from backend.strategy import reward_density

    max_age_hours = _retention_days() * 24.0
    targets: list[tuple[str, Path, str]] = [
        ("conversion_funnel", Path(funnel_path()), "ts"),
        ("reward_ledger", Path(reward_density._persist_path()), "computed_at"),
        ("outreach_interactions", state_path("outreach", "interaction_ledger.jsonl"), "ts"),
        ("experiment_assignments", state_path("experiments", "assignments.jsonl"), "ts"),
        ("experiment_promotions", state_path("experiments", "promotions.jsonl"), "ts"),
        ("voice_call_arms", state_path("voice", "call_arm_ledger.jsonl"), "ts"),
    ]
    rotated: dict[str, int] = {}
    for name, path, ts_field in targets:
        try:
            if not path.exists():
                rotated[name] = 0
                continue
            rotated[name] = JsonlLedger(path).rotate_by_age(
                max_age_hours=max_age_hours, ts_field=ts_field,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("compress: rotate failed %s: %s", name, exc)
            rotated[name] = -1
    guidance_dropped = 0
    try:
        from backend.cognitive.guidance import GuidanceLedger

        guidance_dropped = GuidanceLedger().compact()
    except Exception as exc:  # noqa: BLE001
        log.warning("compress: guidance compact failed: %s", exc)
    return {
        "retention_days": _retention_days(),
        "rotated": rotated,
        "guidance_rows_dropped": guidance_dropped,
    }


# ---------------------------------------------------------------------------
# Stage e — EXPIRE_GUIDANCE (triage-drain for never-triaged recommendations)
# ---------------------------------------------------------------------------
def _guidance_stale_days() -> int:
    """Age threshold (days) past which an untriaged PROPOSED rec is abandoned.

    Wire-not-arm: default 0 (DISABLED). The operator arms the auto-expiry by
    setting ``SAMUS_GUIDANCE_STALE_DAYS`` to a positive integer (e.g. 14). Any
    absent / non-positive / unparseable value keeps the stage a pure no-op.
    """
    raw = (os.getenv(ENV_GUIDANCE_STALE_DAYS) or "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def expire_stale_guidance(day: str) -> dict[str, Any]:
    """Abandon PROPOSED guidance never triaged within N days (wire-not-arm).

    The externally-sourced recommendations (day-start briefing, EOD review,
    CODB reasoner, gameplan corroboration) get NO automated triage and so pile
    up as ``proposed`` forever. This is the SAFE scheduled drain: once a
    PROPOSED rec is at least ``SAMUS_GUIDANCE_STALE_DAYS`` old it is ABANDONED
    with a terminal ``"expired: never triaged"`` outcome, dropping it out of
    ``open_items()`` / the effectiveness summary's ``open_count``.

    SAFE by construction:
      * Only PROPOSED recs are touched — ACCEPTED / IN_PROGRESS / any terminal
        state (incl. the auto-accepted ``distilled-*`` lessons) is left alone.
      * ``abandon()`` is terminal and touches NO effector, send path, or cash
        engine — it only marks the rec closed.
      * It does NOT auto-accept anything (auto-accept would violate the guidance
        module's documented wire-not-arm contract).
      * DISABLED by default (``stale_days <= 0``) — the operator arms it.
    """
    stale_days = _guidance_stale_days()
    if stale_days <= 0:
        return {"enabled": False, "stale_days": stale_days, "abandoned": 0}
    try:
        anchor = date.fromisoformat((day or "").strip() or _today())
    except ValueError:
        anchor = date.fromisoformat(_today())
    cutoff = anchor - timedelta(days=stale_days)

    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.guidance_models import GuidanceStatus

    led = GuidanceLedger()
    scanned = 0
    abandoned = 0
    for rec in led.all_latest():
        if rec.status != GuidanceStatus.PROPOSED.value:
            continue
        scanned += 1
        created = _parse_ts(rec.ts)
        if created is None:
            continue  # unparseable creation ts — leave it rather than guess
        if date.fromisoformat(business_date(created)) > cutoff:
            continue  # younger than the threshold — leave it for triage
        try:
            if led.abandon(rec.recommendation_id, reason="expired: never triaged") is not None:
                abandoned += 1
        except Exception as exc:  # noqa: BLE001 — one bad row is not fatal
            log.warning("expire_stale_guidance: abandon failed %s: %s",
                        rec.recommendation_id, exc)
    log.info("expire_stale_guidance: day=%s stale_days=%d proposed=%d abandoned=%d",
             anchor.isoformat(), stale_days, scanned, abandoned)
    return {
        "enabled": True,
        "stale_days": stale_days,
        "cutoff": cutoff.isoformat(),
        "proposed_scanned": scanned,
        "abandoned": abandoned,
    }


# ---------------------------------------------------------------------------
# Stage f — REDTEAM (Samus-Red adversarial resilience pass)
# ---------------------------------------------------------------------------
def redteam(day: str) -> dict[str, Any]:
    """Run the nightly Samus-Red pass: attack own defenses, score resilience.

    Deterministic + zero-LLM. Breaches are filed into the guidance ledger as
    Blue's remediation work (so REASON + the morning brief surface them next
    cycle); a prior breach that is now contained is closed as a Blue win.
    """
    from backend.redteam.sentinel import run_redteam_pass

    return run_redteam_pass(day)


# ---------------------------------------------------------------------------
# Stage f — HYPOTHESIZE (Concept 7: propose_next_experiment)
# ---------------------------------------------------------------------------
def hypothesize(day: str) -> dict[str, Any]:
    """Draft the next experiment from tonight's observations.

    Closes the observe -> hypothesize -> experiment -> measure -> publish ->
    institutionalize loop: DISTILL/PROMOTE/CALIBRATE have already produced the
    observation surface (stale beliefs + promoted guidance + active-experiment
    coverage); this stage hands them to
    :func:`backend.experiments.registry.propose_next_experiment`, which either
    auto-registers a low-risk proposal or files an ADR-0019 HOTL approval for
    high-risk dimensions (pricing_tier / call_script / cadence / tier-1
    beliefs / high economic impact).

    Deterministic + zero-LLM. Fail-soft — a proposer fault never sinks the
    nightly run, matching every other consolidation stage.
    """
    from backend.experiments.registry import propose_next_experiment

    context: dict[str, Any] = {"day": d if (d := (day or "").strip()) else _today()}
    proposal = propose_next_experiment(context)
    if proposal is None:
        return {"proposed": False, "reason": "no_actionable_observation"}
    return {
        "proposed": True,
        "proposal_id": proposal.proposal_id,
        "dimension": proposal.dimension,
        "candidate_arm": proposal.candidate_arm,
        "status": proposal.status,
        "risk_score": proposal.risk_score,
        "approval_id": proposal.approval_id,
    }


# ---------------------------------------------------------------------------
# The nightly entry point
# ---------------------------------------------------------------------------
def run_consolidation(day: str = "") -> dict[str, Any]:
    """Run all six stages for ``day`` (default: today). Never raises."""
    d = (day or "").strip() or _today()
    out: dict[str, Any] = {"day": d, "started_at": iso_now(), "stages": {}}
    for name, fn in (
        ("distill", lambda: distill(d)),
        ("promote", promote),
        ("calibrate", lambda: calibrate(d)),
        ("expire_guidance", lambda: expire_stale_guidance(d)),
        ("compress", compress),
        ("redteam", lambda: redteam(d)),
        ("hypothesize", lambda: hypothesize(d)),
    ):
        try:
            out["stages"][name] = fn()
        except Exception as exc:  # noqa: BLE001 — every stage is fail-soft
            log.exception("consolidation stage %s faulted", name)
            out["stages"][name] = {"error": str(exc)}
    out["ok"] = all("error" not in (v or {}) for v in out["stages"].values())
    out["finished_at"] = iso_now()
    log.info("consolidation %s: ok=%s", d, out["ok"])
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m backend.cognitive.consolidator [--day YYYY-MM-DD]``."""
    import argparse

    parser = argparse.ArgumentParser(description="Run nightly memory consolidation")
    parser.add_argument("--day", default="", help="ISO date (default: today)")
    args = parser.parse_args(argv)
    result = run_consolidation(args.day)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "run_consolidation",
    "distill",
    "promote",
    "calibrate",
    "expire_stale_guidance",
    "compress",
    "redteam",
    "main",
]
