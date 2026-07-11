"""Unit tests for the conversion-effectiveness checks.

All offline: a FakeProvider injects controlled numbers so each check's
OK/WARN/FAIL/UNKNOWN path is exercised without AWS or a running stack.
"""

from __future__ import annotations

import datetime as _dt


from backend.observability.production_effectiveness import (
    EffStatus,
    check_production_effectiveness,
    to_health_checks,
)


class FakeProvider:
    """Returns whatever the test hands it; None models a data fault."""

    def __init__(self, **vals):
        self._vals = vals

    def prospect_counts(self):
        return self._vals.get("prospect_counts")

    def opportunity_counts(self):
        return self._vals.get("opportunity_counts")

    def last_auto_stake(self):
        return self._vals.get("last_auto_stake")

    def call_pace(self):
        return self._vals.get("call_pace")

    def funnel_counts(self):
        return self._vals.get("funnel_counts")


def _by_name(report):
    return {c.name: c for c in report.checks}


# The production baseline captured on 2026-07-06: everything starved.
STARVED = dict(
    prospect_counts=(2181, 577, 83),  # 74% unscored, 3.8% emailable
    opportunity_counts=(13, 13),  # all 13 open opps are $0
    last_auto_stake=(14, 0),  # scanned 14, staked 0
    call_pace=(1, 30, 0.5),  # 1 call, goal 30, half-day elapsed
    funnel_counts=(13, 0),  # 13 opps, 0 proposals
)


def test_starved_baseline_flags_every_effectiveness_failure():
    report = check_production_effectiveness(FakeProvider(**STARVED))
    checks = _by_name(report)
    assert checks["scoring_coverage"].status is EffStatus.FAIL
    assert checks["contactability"].status is EffStatus.FAIL
    assert checks["staking_throughput"].status is EffStatus.FAIL
    assert checks["opportunity_value"].status is EffStatus.FAIL
    assert checks["call_pace"].status is EffStatus.FAIL
    assert checks["funnel_progression"].status is EffStatus.WARN
    assert report.alerting is True
    assert report.worst is EffStatus.FAIL


def test_healthy_shop_is_all_ok():
    report = check_production_effectiveness(
        FakeProvider(
            prospect_counts=(1000, 950, 800),  # 5% unscored, 80% emailable
            opportunity_counts=(20, 1),  # 1/20 zero-value
            last_auto_stake=(30, 8),  # staking works
            call_pace=(20, 30, 0.5),  # 20 vs 15 pace target
            funnel_counts=(20, 6),  # advancing to proposal
        )
    )
    assert report.alerting is False
    assert report.worst is EffStatus.OK
    assert all(c.status is EffStatus.OK for c in report.checks)


def test_scoring_warn_band():
    # 40% unscored -> WARN (>=0.30, <0.60)
    report = check_production_effectiveness(
        FakeProvider(
            prospect_counts=(100, 60, 90),
        )
    )
    assert _by_name(report)["scoring_coverage"].status is EffStatus.WARN


def test_contactability_warn_band():
    # 20% emailable -> WARN (>=0.10, <0.30)
    report = check_production_effectiveness(
        FakeProvider(
            prospect_counts=(100, 100, 20),
        )
    )
    assert _by_name(report)["contactability"].status is EffStatus.WARN


def test_staking_ok_when_some_staked():
    report = check_production_effectiveness(
        FakeProvider(
            last_auto_stake=(14, 3),
        )
    )
    assert _by_name(report)["staking_throughput"].status is EffStatus.OK


def test_opportunity_value_partial_is_warn():
    # half the open opps are $0 -> WARN, not FAIL
    report = check_production_effectiveness(
        FakeProvider(
            opportunity_counts=(10, 5),
        )
    )
    assert _by_name(report)["opportunity_value"].status is EffStatus.WARN


def test_missing_data_degrades_to_unknown_not_crash():
    # Provider returns None for everything (total data outage).
    report = check_production_effectiveness(FakeProvider())
    statuses = {c.status for c in report.checks}
    assert statuses == {EffStatus.UNKNOWN}
    # UNKNOWN is not alert-worthy on its own.
    assert report.alerting is False


def test_provider_exception_is_swallowed_fail_open():
    class Boom:
        def prospect_counts(self):
            raise RuntimeError("ddb down")

        def opportunity_counts(self):
            raise RuntimeError("ddb down")

        def last_auto_stake(self):
            raise RuntimeError("ledger down")

        def call_pace(self):
            raise RuntimeError("crm down")

        def funnel_counts(self):
            raise RuntimeError("ddb down")

    report = check_production_effectiveness(Boom())  # must not raise
    assert all(c.status is EffStatus.UNKNOWN for c in report.checks)


def test_empty_stores_are_info_not_fail():
    report = check_production_effectiveness(
        FakeProvider(
            prospect_counts=(0, 0, 0),
            opportunity_counts=(0, 0),
            funnel_counts=(0, 0),
            call_pace=(0, 0, 0.5),
        )
    )
    checks = _by_name(report)
    assert checks["scoring_coverage"].status is EffStatus.INFO
    assert checks["contactability"].status is EffStatus.INFO
    assert checks["opportunity_value"].status is EffStatus.INFO
    assert checks["funnel_progression"].status is EffStatus.INFO
    assert checks["call_pace"].status is EffStatus.INFO


def test_call_pace_scales_with_day_fraction():
    # 10 calls, goal 30. Early in the day (10% elapsed -> target 3) => OK.
    early = check_production_effectiveness(FakeProvider(call_pace=(10, 30, 0.1)))
    assert _by_name(early)["call_pace"].status is EffStatus.OK
    # Same 10 calls late in the day (100% elapsed -> target 30) => FAIL.
    late = check_production_effectiveness(FakeProvider(call_pace=(3, 30, 1.0)))
    assert _by_name(late)["call_pace"].status is EffStatus.FAIL


def test_report_serialization_and_adapter():
    report = check_production_effectiveness(
        FakeProvider(**STARVED),
        now=_dt.datetime(2026, 7, 6, 16, 0, 0, tzinfo=_dt.timezone.utc),
    )
    d = report.to_dict()
    assert d["generated_at"] == "2026-07-06T16:00:00Z"
    assert d["alerting"] is True
    assert d["worst"] == "fail"
    assert len(d["checks"]) == 6
    # Adapter yields the liveness-monitor (name, status) shape.
    flat = to_health_checks(report)
    assert {"name", "status", "detail"} <= set(flat[0].keys())
    assert len(flat) == 6
