"""Causal uplift — turn the relative bandit into a "does it beat the incumbent?".

The UCB1 bandit (backend/attribution + backend/strategy) and the nightly
promoter answer a *relative* question: which arm has the highest win rate? That
is correlational — it will happily promote an arm that merely got lucky, or that
rode a favourable market window, over the incumbent. This module answers the
*causal* question the promoter cannot: **does this arm significantly beat the
control (baseline) arm, and how confident are we the lift is real rather than
noise or a confounder?**

It is a thin, stdlib-only overlay on the data the bandit already records
(``registry.arm_stats`` -> ``{arm: {wins, trials}}``) and the two-proportion
z-test already used for promotion (``significance.py``). No new store, no scipy.

For each non-control arm we report the treatment effect vs the control:

  * ``absolute_uplift`` = arm_rate - control_rate
  * ``relative_uplift`` = absolute_uplift / control_rate
  * ``p_value`` / ``confidence`` (= 1 - p) from the two-proportion z-test
  * ``significant`` at ``alpha``
  * ``spurious_risk`` in {low, medium, high} — the honest, cheap confounder
    guard: HIGH when the sample is thin or the lift is not significant, so an
    operator (or the uplift-gated promoter) never treats noise as a real effect.

Confounder note: a fuller design time-buckets arm-vs-control so a seasonal swing
cannot masquerade as an arm effect. That needs per-window stats the assignments
ledger can supply; this first cut uses the sample-size + significance guard,
which already prevents the dominant failure mode (promoting a small-sample fluke).
"""

from __future__ import annotations

from typing import Any, Mapping

from . import registry
from .significance import p_value_two_sided, two_proportion_z

__all__ = [
    "uplift_report",
    "best_causal_arm",
    "SPURIOUS_LOW",
    "SPURIOUS_MEDIUM",
    "SPURIOUS_HIGH",
    "DEFAULT_MIN_SAMPLE",
]

SPURIOUS_LOW = "low"
SPURIOUS_MEDIUM = "medium"
SPURIOUS_HIGH = "high"

# Below this per-arm trial count, any lift is treated as sample-thin (high risk
# of a fluke / unmodelled confounder). Mirrors the promoter's min_trials intent.
DEFAULT_MIN_SAMPLE = 30
DEFAULT_ALPHA = 0.05


def _rate(stats: Mapping[str, Any]) -> float:
    trials = int(stats.get("trials", 0) or 0)
    return (int(stats.get("wins", 0) or 0) / trials) if trials > 0 else 0.0


def _resolve_control(exp: registry.Experiment, stats: Mapping[str, Any]) -> str | None:
    """The control arm: the explicit ``control_arm`` if live, else the arm with
    the most trials (the de-facto incumbent). None when there are no arms."""
    archived = set(exp.archived_arms)
    live = [a for a in exp.arms if a not in archived]
    if not live:
        return None
    if exp.control_arm and exp.control_arm in live:
        return exp.control_arm
    return max(live, key=lambda a: int((stats.get(a) or {}).get("trials", 0) or 0))


def _spurious_risk(
    arm_stats: Mapping[str, Any],
    control_stats: Mapping[str, Any],
    p_value: float,
    alpha: float,
    min_sample: int,
) -> str:
    """Cheap confounder guard: thin sample OR non-significant lift -> high risk."""
    arm_trials = int(arm_stats.get("trials", 0) or 0)
    ctrl_trials = int(control_stats.get("trials", 0) or 0)
    if arm_trials < min_sample or ctrl_trials < min_sample:
        return SPURIOUS_HIGH
    if p_value >= alpha:
        return SPURIOUS_HIGH
    # Significant + adequate sample, but a borderline p-value still warrants a
    # medium flag so a marginal result isn't over-trusted.
    if p_value >= alpha / 5.0:
        return SPURIOUS_MEDIUM
    return SPURIOUS_LOW


def uplift_report(
    experiment_id: str,
    *,
    control_arm: str | None = None,
    alpha: float = DEFAULT_ALPHA,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-arm causal uplift vs the control for one experiment.

    ``stats`` may be injected (``{arm: {wins, trials}}``) for testing; otherwise
    it is read from the bandit store via ``registry.arm_stats``. Returns a
    structured dict and never raises — an unknown experiment yields
    ``{"ok": False, "error": ...}``.
    """
    exp = registry.get_experiment(experiment_id)
    if exp is None:
        return {"ok": False, "error": f"unknown experiment {experiment_id!r}"}

    arm_stats = dict(stats) if stats is not None else registry.arm_stats(experiment_id)
    control = control_arm or _resolve_control(exp, arm_stats)
    if not control:
        return {"ok": False, "error": "no live arms to establish a control"}

    control_stats = arm_stats.get(control) or {"wins": 0, "trials": 0}
    control_rate = _rate(control_stats)
    c_wins = int(control_stats.get("wins", 0) or 0)
    c_trials = int(control_stats.get("trials", 0) or 0)

    archived = set(exp.archived_arms)
    arms_out: list[dict[str, Any]] = []
    for arm in exp.arms:
        if arm == control or arm in archived:
            continue
        s = arm_stats.get(arm) or {"wins": 0, "trials": 0}
        a_wins = int(s.get("wins", 0) or 0)
        a_trials = int(s.get("trials", 0) or 0)
        arm_rate = _rate(s)
        z = two_proportion_z(a_wins, a_trials, c_wins, c_trials)
        p = p_value_two_sided(z)
        absolute = arm_rate - control_rate
        relative = (absolute / control_rate) if control_rate > 0 else 0.0
        risk = _spurious_risk(s, control_stats, p, alpha, min_sample)
        arms_out.append(
            {
                "arm": arm,
                "arm_rate": round(arm_rate, 4),
                "control_rate": round(control_rate, 4),
                "absolute_uplift": round(absolute, 4),
                "relative_uplift": round(relative, 4),
                "z": round(z, 4),
                "p_value": round(p, 4),
                "confidence": round(1.0 - p, 4),
                "significant": bool(p < alpha and absolute > 0),
                "spurious_risk": risk,
                "trials": a_trials,
            }
        )

    # Rank by absolute uplift, most positive first.
    arms_out.sort(key=lambda a: a["absolute_uplift"], reverse=True)
    return {
        "ok": True,
        "experiment_id": experiment_id,
        "dimension": exp.dimension,
        "control_arm": control,
        "control_rate": round(control_rate, 4),
        "alpha": alpha,
        "min_sample": min_sample,
        "arms": arms_out,
    }


def best_causal_arm(
    experiment_id: str,
    *,
    alpha: float = DEFAULT_ALPHA,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The single arm with a SIGNIFICANT, low-spurious-risk positive uplift over
    control, or None if no arm causally beats the incumbent. This is the gate a
    causally-honest promoter consults before crowning a winner."""
    report = uplift_report(
        experiment_id,
        alpha=alpha,
        min_sample=min_sample,
        stats=stats,
    )
    if not report.get("ok"):
        return None
    for arm in report["arms"]:  # already sorted best-uplift-first
        if arm["significant"] and arm["spurious_risk"] != SPURIOUS_HIGH:
            return arm
    return None
