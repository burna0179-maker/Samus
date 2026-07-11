"""Morning brief ORGANIZATIONAL ECONOMICS section (Concept 4 wiring).

Focuses on the Samus-side reader supplier :func:`morning._samus_saturation_reader`
and the section renderer :func:`morning._render_organizational_economics`.

`regret_reader` is intentionally NOT supplied (per module docstring: the
RegretLedger has no persistence layer today, so passing a hardcoded zero
would fake resolution when the truth is 'no data yet'). This test file
codifies that expectation so a future change can't silently break the
honest-degradation contract.
"""
from __future__ import annotations

from backend import morning


def test_saturation_reader_empty_when_no_active_experiments(monkeypatch):
    monkeypatch.setattr(
        "backend.experiments.registry.list_experiments",
        lambda *, status=None: [],
    )
    assert morning._samus_saturation_reader() == {}


def test_saturation_reader_skips_zero_trial_experiments(monkeypatch):
    class _Exp:
        def __init__(self, experiment_id, dimension):
            self.experiment_id = experiment_id
            self.dimension = dimension

    monkeypatch.setattr(
        "backend.experiments.registry.list_experiments",
        lambda *, status=None: [_Exp("empty-1", "pricing_tier")],
    )
    monkeypatch.setattr(
        "backend.experiments.registry.arm_stats",
        lambda experiment_id: {
            "A": {"trials": 0, "wins": 0, "mean_reward": 0.0},
            "B": {"trials": 0, "wins": 0, "mean_reward": 0.0},
        },
    )
    # zero total trials -> dimension is not included -> empty map
    assert morning._samus_saturation_reader() == {}


def test_saturation_reader_returns_risk_map_from_real_trials(monkeypatch):
    class _Exp:
        def __init__(self, experiment_id, dimension):
            self.experiment_id = experiment_id
            self.dimension = dimension

    experiments = [
        _Exp("pt-01", "pricing_tier"),
        _Exp("pt-02", "pricing_tier"),  # same dimension, trials should sum
        _Exp("cs-01", "call_script"),
    ]
    stats_by_experiment = {
        "pt-01": {"A": {"trials": 5}, "B": {"trials": 5}},   # 10 total
        "pt-02": {"A": {"trials": 40}},                       # 40 total
        "cs-01": {"A": {"trials": 3}, "B": {"trials": 2}},   # 5 total
    }
    monkeypatch.setattr(
        "backend.experiments.registry.list_experiments",
        lambda *, status=None: experiments,
    )
    monkeypatch.setattr(
        "backend.experiments.registry.arm_stats",
        lambda experiment_id: stats_by_experiment.get(experiment_id, {}),
    )
    result = morning._samus_saturation_reader()
    # both dimensions get a risk score (pricing_tier has 50/55 share -> high;
    # call_script has 5/55 -> below the fair-share floor -> 0.0)
    assert set(result.keys()) == {"pricing_tier", "call_script"}
    assert result["pricing_tier"] > result["call_script"]
    for risk in result.values():
        assert 0.0 <= risk <= 1.0


def test_saturation_reader_fail_open_on_arm_stats_exception(monkeypatch):
    class _Exp:
        def __init__(self, experiment_id, dimension):
            self.experiment_id = experiment_id
            self.dimension = dimension

    def boom(*a, **kw):
        raise RuntimeError("arm_stats blew up")

    monkeypatch.setattr(
        "backend.experiments.registry.list_experiments",
        lambda *, status=None: [_Exp("x", "pricing_tier")],
    )
    monkeypatch.setattr(
        "backend.experiments.registry.arm_stats",
        boom,
    )
    # per-experiment exception is contained -> empty map, never raises
    assert morning._samus_saturation_reader() == {}


def test_saturation_reader_fail_open_on_list_experiments_exception(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("list blew up")

    monkeypatch.setattr(
        "backend.experiments.registry.list_experiments",
        boom,
    )
    assert morning._samus_saturation_reader() == {}


def test_render_organizational_economics_uses_supplied_saturation_reader(monkeypatch):
    """The renderer wires _samus_saturation_reader so cognitive_overhead
    flips from sources_missing=True to a real value once experiments have trials.
    """
    monkeypatch.setenv("NO_COLOR", "1")

    class _Exp:
        def __init__(self, experiment_id, dimension):
            self.experiment_id = experiment_id
            self.dimension = dimension

    monkeypatch.setattr(
        "backend.experiments.registry.list_experiments",
        lambda *, status=None: [_Exp("pt-01", "pricing_tier")],
    )
    monkeypatch.setattr(
        "backend.experiments.registry.arm_stats",
        lambda experiment_id: {"A": {"trials": 50}, "B": {"trials": 50}},
    )

    lines = morning._render_organizational_economics()
    assert lines, "renderer should surface metrics when the reader supplies data"
    joined = "\n".join(lines)
    assert "ORGANIZATIONAL ECONOMICS" in joined
    assert "cognitive_overhead" in joined
    # cognitive_overhead should NOT be marked degraded now that we have a
    # supplier; the marker "  !" is only prepended when sources_missing.
    for line in lines:
        if "cognitive_overhead" in line:
            assert not line.strip().startswith("!"), (
                f"cognitive_overhead should not be marked degraded when a "
                f"real saturation reader is wired: {line!r}"
            )


def test_render_organizational_economics_regret_stays_degraded(monkeypatch):
    """`regret_reader` is intentionally unsupplied because RegretLedger has no
    persistence layer today - the honest signal is 'no data yet', not zero.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    # Neutralise saturation so the assertion focuses on regret
    monkeypatch.setattr(morning, "_samus_saturation_reader", lambda: {})

    lines = morning._render_organizational_economics()
    joined = "\n".join(lines)
    assert "communication_entropy" in joined
    # Locate the communication_entropy row + its detail line, confirm the
    # degraded marker is present (rendered "  !" before the metric name).
    found_degraded = False
    for i, line in enumerate(lines):
        if "communication_entropy" in line:
            # the marker sits at the start of the metric row (line.lstrip
            # strips the color escapes / whitespace in NO_COLOR mode)
            if line.lstrip().startswith("!"):
                found_degraded = True
            break
    assert found_degraded, (
        "communication_entropy should still be marked degraded until "
        "regret_reader is wired to real telemetry"
    )
