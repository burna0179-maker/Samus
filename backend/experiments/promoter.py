"""Nightly experiment promotion / demotion + campaign stop rule (Tranche 3).

For every ACTIVE experiment whose arms have accumulated ``min_trials``:

  * **Winner** (significantly better win rate than the runner-up): its
    allocation floor is raised and it is registered as the dimension's
    template default (see :func:`register_template_default` — NOTE:
    ``backend/scaffold/templates.py`` has no extension point today, so the
    default is registered in a JSON template-defaults store that template
    consumers read; the scaffold read-side integration lands at merge).
  * **Loser** (significantly below the best arm): archived (status change on
    the experiment record — its bandit history stays in the ledger/store) and
    ONE replacement variant is generated via a metered LLM call through
    :mod:`backend.common.llm_client` (deterministic template fallback when
    the budget denies or the call fails).

Campaign stop rule: when the conversion funnel's overall rate is below
threshold with sufficient sample, a halt flag is written and an operator task
is created through the CRM operator-task path.

Every promotion/demotion is appended to a promotions JSONL ledger and emitted
as a ``decision.made`` unified business event (via the Tranche-1 shim).
All effects are fail-soft; the nightly run never raises.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.common.business_events_shim_t3 import emit_business_event
from backend.common.dates import iso_now
from backend.common.state_paths import state_path

from . import registry
from .significance import is_significant

_LOG = logging.getLogger("samus.experiments.promoter")

# --- tunables (env-overridable, sensible defaults) --------------------------
ENV_STOP_CONVERSION_RATE = "SAMUS_EXP_STOP_CONVERSION_RATE"
ENV_STOP_MIN_SAMPLE = "SAMUS_EXP_STOP_MIN_SAMPLE"
ENV_ALPHA = "SAMUS_EXP_SIGNIFICANCE_ALPHA"
# When enabled, a winner must ALSO significantly beat the control arm (causal
# uplift), not just the runner-up — prevents promoting a lucky relative winner.
# Default OFF: the causal uplift is always computed + recorded for audit, but
# gating the promotion decision on it is an explicit operator opt-in.
ENV_UPLIFT_GATE = "SAMUS_EXP_UPLIFT_GATE"

DEFAULT_STOP_CONVERSION_RATE = 0.01   # overall closed_won/lead below this...
DEFAULT_STOP_MIN_SAMPLE = 50          # ...with at least this many leads -> halt
DEFAULT_ALPHA = 0.05
WINNER_ALLOCATION_FLOOR = 0.5

_LLM_WORKCELL = "experiments"
_LLM_MAX_TOKENS = 200

_PROMOTIONS_JSONL = ("experiments", "promotions.jsonl")
_TEMPLATE_DEFAULTS_JSON = ("experiments", "template_defaults.json")
_CAMPAIGN_HALT_JSON = ("experiments", "campaign_halt.json")
# Structured strategy-compression rollup (Concept 2 — "wisdom over knowledge").
# Each row is one promoted pattern rendered as a queryable business-law
# record: what to do (law text), how well it's supported (evidence_count,
# confidence), and where it came from (source_pattern_ids). Downstream readers
# treat these as durable strategy, not raw operational exhaust.
_GUIDANCE_LAWS_JSONL = ("compression", "guidance_laws.jsonl")


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOG.warning("ignoring malformed %s=%r", name, raw)
        return default


def _promotions_ledger():
    from backend.common.persistence import open_ledger

    return open_ledger(
        jsonl_path=state_path(*_PROMOTIONS_JSONL),
        collection="experiment_promotions",
    )


def _append_promotion(row: dict[str, Any]) -> None:
    try:
        _promotions_ledger().append(row)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("promotions ledger append failed: %s", exc)
    emit_business_event(
        "decision.made",
        workcell="experiments",
        variant_arm_id=row.get("arm_id"),
        metadata=row,
    )


# ---------------------------------------------------------------------------
# Template defaults registry (winner -> library default)
# ---------------------------------------------------------------------------
def _template_defaults_path() -> Path:
    return state_path(*_TEMPLATE_DEFAULTS_JSON)


def register_template_default(dimension: str, experiment_id: str, arm: str) -> bool:
    """Record ``arm`` as the library default for ``dimension``.

    ``scaffold/templates.py`` exposes no registration hook (its renderer map
    is module-private), so defaults live in this JSON store; template
    consumers call :func:`template_default` — the scaffold read-side wiring
    lands at merge with the sibling branches.
    """
    path = _template_defaults_path()
    try:
        data: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        data[dimension] = {
            "experiment_id": experiment_id,
            "arm": arm,
            "ts": iso_now(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError) as exc:
        _LOG.warning("template default write failed %s: %s", dimension, exc)
        return False


def template_default(dimension: str) -> dict[str, Any] | None:
    """The promoted default for ``dimension``, or None. Fail-open."""
    path = _template_defaults_path()
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = (data or {}).get(dimension)
        return dict(entry) if isinstance(entry, dict) else None
    except (OSError, ValueError) as exc:
        _LOG.warning("template default read failed %s: %s", dimension, exc)
        return None


# ---------------------------------------------------------------------------
# Guidance laws — structured strategy-compression rollup (Concept 2)
# ---------------------------------------------------------------------------
@dataclass
class GuidanceLaw:
    """One durable strategy rule promoted from operational exhaust.

    The *what*: ``law`` reads like actionable strategy the operator (or a
    downstream planner) can consume without re-deriving it — e.g. *"Independent
    dental practices respond best to Tue-Thu 8-10am local, authority-first
    messaging"*.

    The *how sure*: ``evidence_count`` is the number of underlying observations
    (trials / interactions / rows) that support the law; ``confidence`` is a
    0..1 rough posterior on the same evidence.

    The *where from*: ``source_pattern_ids`` cite the concrete lesson /
    promotion / experiment identifiers so a reader can retrace the derivation.
    """

    law_id: str
    law: str
    evidence_count: int
    confidence: float
    promoted_at: str
    source_pattern_ids: list[str] = field(default_factory=list)
    category: str = ""


def _guidance_laws_ledger():
    from backend.common.persistence import open_ledger

    return open_ledger(
        jsonl_path=state_path(*_GUIDANCE_LAWS_JSONL),
        collection="guidance_laws",
    )


def emit_guidance_law(law: GuidanceLaw) -> bool:
    """Append one :class:`GuidanceLaw` to the compression rollup. Fail-soft.

    Returns True iff the row was persisted. Also fires a ``law.promoted``
    unified business event so downstream subscribers (guidance ingest,
    morning brief) see the same "promoted" moment the promoter and
    consolidator do.
    """
    row = asdict(law)
    ok = True
    try:
        _guidance_laws_ledger().append(row)
    except Exception as exc:  # noqa: BLE001 — rollup never sinks the caller
        _LOG.warning("guidance_laws append failed: %s", exc)
        ok = False
    try:
        emit_business_event("law.promoted", workcell="experiments", metadata=row)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("law.promoted event emit failed: %s", exc)
    return ok


def read_guidance_laws(limit: int = 50) -> list[GuidanceLaw]:
    """Replay the compression rollup, oldest-first. Fail-open (empty list).

    ``limit`` bounds the tail scan so a large rollup does not fault a
    latency-sensitive caller (e.g. morning brief composition).
    """
    try:
        rows = _guidance_laws_ledger().tail(limit=limit)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("guidance_laws read failed: %s", exc)
        return []
    out: list[GuidanceLaw] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(GuidanceLaw(
                law_id=str(row.get("law_id") or ""),
                law=str(row.get("law") or ""),
                evidence_count=int(row.get("evidence_count") or 0),
                confidence=float(row.get("confidence") or 0.0),
                promoted_at=str(row.get("promoted_at") or ""),
                source_pattern_ids=[
                    str(pid) for pid in (row.get("source_pattern_ids") or [])
                ],
                category=str(row.get("category") or ""),
            ))
        except (TypeError, ValueError) as exc:
            _LOG.warning("guidance_laws row skipped (bad shape): %s", exc)
            continue
    return out


# ---------------------------------------------------------------------------
# Replacement variant generation (metered LLM, template fallback)
# ---------------------------------------------------------------------------
def _fallback_variant(loser_arm: str) -> str:
    return f"{loser_arm}-alt-{uuid.uuid4().hex[:6]}"


def _generate_replacement(exp: registry.Experiment, loser_arm: str) -> str:
    """ONE metered LLM call for a replacement variant; fallback on any denial."""
    try:
        from backend.common.llm_client import anthropic_messages

        prompt = (
            f"An A/B experiment over the '{exp.dimension}' dimension of a "
            f"local-SEO agency's sales motion just archived the losing variant "
            f"'{loser_arm}'. Surviving variants: "
            f"{[a for a in exp.arms if a != loser_arm]}. "
            "Propose ONE new candidate variant as a short slug (lowercase, "
            "hyphenated, under 6 words) that is meaningfully different from "
            "the survivors. Reply with the slug only."
        )
        text, _usage = anthropic_messages(
            workcell=_LLM_WORKCELL,
            api_key="unused",
            prompt=prompt,
            max_tokens=_LLM_MAX_TOKENS,
        )
        slug = "".join(
            ch if (ch.isalnum() or ch == "-") else "-"
            for ch in (text or "").strip().splitlines()[0].strip().lower()
        ).strip("-")
        if slug and slug not in exp.arms and len(slug) <= 64:
            return slug
        return _fallback_variant(loser_arm)
    except Exception as exc:  # noqa: BLE001 — budget denial / transport / parse
        _LOG.info("replacement LLM denied/failed (%s); template fallback", exc)
        return _fallback_variant(loser_arm)


# ---------------------------------------------------------------------------
# Campaign stop rule
# ---------------------------------------------------------------------------
def check_campaign_stop() -> dict[str, Any]:
    """Halt when funnel conversion is below threshold with sufficient sample.

    Writes a sticky halt flag (``experiments/campaign_halt.json``) and creates
    an operator task via the CRM operator-task path. The flag stays until the
    operator clears the file — the promoter never un-halts on its own.
    """
    threshold = _env_float(ENV_STOP_CONVERSION_RATE, DEFAULT_STOP_CONVERSION_RATE)
    min_sample = int(_env_float(ENV_STOP_MIN_SAMPLE, DEFAULT_STOP_MIN_SAMPLE))
    try:
        from backend.common.conversion_funnel import funnel_snapshot

        snap = funnel_snapshot()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("stop-rule funnel read failed: %s", exc)
        return {"halted": False, "error": f"funnel_read_failed: {exc}"}

    leads = int((snap.get("stages") or {}).get("lead", 0) or 0)
    rate = float(snap.get("overall_conversion_rate", 0.0) or 0.0)
    if leads < min_sample or rate >= threshold:
        return {
            "halted": False, "leads": leads,
            "conversion_rate": rate, "threshold": threshold,
        }

    reason = (
        f"campaign stop rule: overall conversion {rate:.4f} < {threshold:.4f} "
        f"with {leads} leads (min sample {min_sample})"
    )
    halt = {"halted": True, "reason": reason, "ts": iso_now(),
            "leads": leads, "conversion_rate": rate, "threshold": threshold}
    try:
        path = state_path(*_CAMPAIGN_HALT_JSON)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(halt, indent=2), encoding="utf-8")
    except OSError as exc:
        _LOG.warning("halt flag write failed: %s", exc)
    try:
        from backend.crm.models import CreateOperatorTaskRequest
        from backend.crm.service import create_operator_task

        create_operator_task(CreateOperatorTaskRequest(
            kind="other",
            title="Campaign halted by experiment stop rule — review funnel",
            description=reason,
            source="experiments_promoter",
            source_ref="campaign_stop_rule",
        ))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("halt operator task creation failed: %s", exc)
    _append_promotion({
        "ts": halt["ts"], "kind": "campaign_halt", "reason": reason,
        "leads": leads, "conversion_rate": rate,
    })
    return halt


def campaign_halted() -> bool:
    """True when the sticky halt flag is set. Fail-open (False)."""
    try:
        path = state_path(*_CAMPAIGN_HALT_JSON)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("halted"))
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# The nightly run
# ---------------------------------------------------------------------------
def _win_rate(stats: dict[str, Any]) -> float:
    trials = int(stats.get("trials", 0) or 0)
    return (int(stats.get("wins", 0) or 0) / trials) if trials > 0 else 0.0


def _uplift_gate_enabled() -> bool:
    return (os.getenv(ENV_UPLIFT_GATE) or "").strip().lower() in ("1", "true", "yes", "on")


def run_nightly_promotion(*, alpha: float | None = None) -> dict[str, Any]:
    """Promote winners / demote losers across all active experiments.

    Returns a per-experiment summary. Never raises; per-experiment failures
    are recorded in the summary and the run continues.
    """
    a = alpha if alpha is not None else _env_float(ENV_ALPHA, DEFAULT_ALPHA)
    summary: dict[str, Any] = {
        "ts": iso_now(), "alpha": a,
        "experiments": {}, "promoted": [], "archived": [],
    }
    for exp in registry.list_experiments(status="active"):
        try:
            summary["experiments"][exp.experiment_id] = _promote_one(exp, a, summary)
        except Exception as exc:  # noqa: BLE001 — one experiment never sinks the run
            _LOG.warning("promotion failed for %s: %s", exp.experiment_id, exc)
            summary["experiments"][exp.experiment_id] = {"error": str(exc)}
    summary["campaign_stop"] = check_campaign_stop()
    return summary


def _promote_one(
    exp: registry.Experiment, alpha: float, summary: dict[str, Any],
) -> dict[str, Any]:
    stats = registry.arm_stats(exp.experiment_id)
    live = [a for a in exp.arms if a not in set(exp.archived_arms)]
    eligible = [a for a in live if stats.get(a, {}).get("trials", 0) >= exp.min_trials]
    result: dict[str, Any] = {
        "live_arms": live, "eligible": eligible,
        "winner": None, "archived": [],
    }
    if len(eligible) < 2:
        result["skipped"] = "fewer than 2 arms past min_trials"
        return result

    ranked = sorted(eligible, key=lambda arm: _win_rate(stats[arm]), reverse=True)
    best, runner_up = ranked[0], ranked[1]

    # Causal overlay: uplift of each arm vs the control (incumbent), so a
    # promotion is auditable as a real treatment effect, not just a relative
    # win. Always computed + attached; optionally GATES promotion.
    causal_best_arm = None
    try:
        from . import uplift as _uplift

        result["uplift"] = _uplift.uplift_report(exp.experiment_id, alpha=alpha, stats=stats)
        _cb = _uplift.best_causal_arm(exp.experiment_id, alpha=alpha, stats=stats)
        causal_best_arm = _cb["arm"] if _cb else None
        result["causal_best_arm"] = causal_best_arm
    except Exception as exc:  # noqa: BLE001 — causal overlay never sinks promotion
        result["uplift_error"] = str(exc)

    gate_on = _uplift_gate_enabled()
    best_beats_control = (causal_best_arm == best)

    changed = False
    # --- winner: significant vs runner-up (and, when gated, vs control) ------
    if (
        is_significant(stats[best], stats[runner_up], alpha=alpha)
        and _win_rate(stats[best]) > _win_rate(stats[runner_up])
        and (not gate_on or best_beats_control)
    ):
        exp.allocation_floors[best] = max(
            exp.allocation_floors.get(best, 0.0), WINNER_ALLOCATION_FLOOR,
        )
        register_template_default(exp.dimension, exp.experiment_id, best)
        result["winner"] = best
        summary["promoted"].append(f"{exp.experiment_id}::{best}")
        changed = True
        _append_promotion({
            "ts": iso_now(), "kind": "winner_promoted",
            "experiment_id": exp.experiment_id, "dimension": exp.dimension,
            "arm": best,
            "arm_id": registry.build_experiment_arm_id(exp.experiment_id, best),
            "win_rate": round(_win_rate(stats[best]), 4),
            "allocation_floor": exp.allocation_floors[best],
            "causal_uplift": next(
                (a for a in (result.get("uplift") or {}).get("arms", []) if a["arm"] == best),
                None,
            ),
            "uplift_gated": gate_on,
        })

    # --- losers: significantly below the best arm -> archive + replace
    for arm in eligible:
        if arm == best or arm in set(exp.archived_arms):
            continue
        if _win_rate(stats[arm]) < _win_rate(stats[best]) and is_significant(
            stats[best], stats[arm], alpha=alpha,
        ):
            exp.archived_arms.append(arm)
            replacement = _generate_replacement(exp, arm)
            if replacement not in exp.arms:
                exp.arms.append(replacement)  # enters as a cold arm
            result["archived"].append({"arm": arm, "replacement": replacement})
            summary["archived"].append(f"{exp.experiment_id}::{arm}")
            changed = True
            _append_promotion({
                "ts": iso_now(), "kind": "loser_archived",
                "experiment_id": exp.experiment_id, "dimension": exp.dimension,
                "arm": arm,
                "arm_id": registry.build_experiment_arm_id(exp.experiment_id, arm),
                "win_rate": round(_win_rate(stats[arm]), 4),
                "replacement": replacement,
            })

    if changed:
        registry.save_experiment(exp)
    return result


__all__ = [
    "run_nightly_promotion",
    "check_campaign_stop",
    "campaign_halted",
    "register_template_default",
    "template_default",
    "GuidanceLaw",
    "emit_guidance_law",
    "read_guidance_laws",
    "WINNER_ALLOCATION_FLOOR",
    "DEFAULT_STOP_CONVERSION_RATE",
    "DEFAULT_STOP_MIN_SAMPLE",
]
