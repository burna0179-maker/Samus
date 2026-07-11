"""Org-debt aggregate (T3) + control-loop friction metrics (T5)."""
from __future__ import annotations

from types import SimpleNamespace

from backend.entropy import friction
from backend.governance import org_debt


# ---------------------------------------------------------------------------
# T3 — org_debt
# ---------------------------------------------------------------------------

class _FakeKarma:
    def __init__(self, v: float) -> None:
        self.v = v

    def dims(self):
        return {"success_rate": self.v, "policy_compliance": self.v,
                "resource_efficiency": self.v, "stability_score": self.v}


class _FakeKarmaStore:
    def __init__(self, m):
        self.m = m

    def load(self, wc):
        return _FakeKarma(self.m.get(wc, 0.5))


class _FakeBudgetStore:
    def __init__(self, m):
        self.m = m

    def snapshot(self, wc):
        return self.m[wc]


def _budget(eff, *, circuit="", errors=0):
    return SimpleNamespace(efficiency_ema=eff, circuit_open_until=circuit,
                           consecutive_errors=errors)


def test_healthy_workcell_has_low_debt():
    karma = _FakeKarmaStore({"seo": 0.95})
    budget = _FakeBudgetStore({"seo": _budget(0.95)})
    row = org_debt.workcell_debt("seo", karma_store=karma, budget_store=budget)
    # 0.4*0.05 + 0.4*0.05 + 0.2*0 = 0.04
    assert row["org_debt"] < 0.1


def test_struggling_workcell_has_high_debt():
    karma = _FakeKarmaStore({"outreach": 0.2})
    budget = _FakeBudgetStore({"outreach": _budget(0.1, circuit="2026-07-06T00:00:00Z")})
    row = org_debt.workcell_debt("outreach", karma_store=karma, budget_store=budget)
    # 0.4*0.8 + 0.4*0.9 + 0.2*1.0 = 0.88
    assert row["org_debt"] > 0.8
    assert row["circuit_penalty"] == 1.0


def test_circuit_penalty_scales_with_errors():
    karma = _FakeKarmaStore({"crm": 0.5})
    budget = _FakeBudgetStore({"crm": _budget(0.5, errors=5)})  # 5/10 = 0.5
    row = org_debt.workcell_debt("crm", karma_store=karma, budget_store=budget)
    assert row["circuit_penalty"] == 0.5


def test_report_ranks_worst_first():
    karma = _FakeKarmaStore({"a": 0.9, "b": 0.2})
    budget = _FakeBudgetStore({"a": _budget(0.9), "b": _budget(0.2)})
    rep = org_debt.org_debt_report(["a", "b"], karma_store=karma, budget_store=budget)
    assert rep["worst"] == "b"
    assert rep["workcells"][0]["workcell"] == "b"
    assert rep["total_org_debt"] > 0


def test_missing_stores_degrade_to_neutral():
    class _Boom:
        def load(self, wc): raise RuntimeError("down")
        def snapshot(self, wc): raise RuntimeError("down")

    row = org_debt.workcell_debt("x", karma_store=_Boom(), budget_store=_Boom())
    # neutral: karma 0.5, eff 1.0, circuit 0 -> 0.4*0.5 + 0 + 0 = 0.2
    assert row["org_debt"] == 0.2


# ---------------------------------------------------------------------------
# T5 — friction
# ---------------------------------------------------------------------------

def _tick(*cut_workcells, extra_adjust=()):
    """A tick whose recommendations cut the given workcells (+ optional non-cut
    adjustments, e.g. priority boosts, to inflate coordination cost)."""
    adj = [{"workcell": w, "quota_cut": True, "priority_boosted": False} for w in cut_workcells]
    adj += [{"workcell": w, "quota_cut": False, "priority_boosted": True} for w in extra_adjust]
    return {"recommendations": {"workcell_adjustments": adj}}


def test_empty_ledger_is_zero_friction():
    rep = friction.friction_report(ticks=[])
    assert rep["ticks_analyzed"] == 0
    assert rep["decision_entropy"] == 0.0
    assert rep["energy_leak"] is False


def test_stable_decisions_have_low_entropy():
    # prospecting cut every tick -> never flips.
    ticks = [_tick("prospecting") for _ in range(5)]
    rep = friction.friction_report(ticks=ticks)
    assert rep["per_workcell"]["prospecting"]["flips"] == 0
    assert rep["decision_entropy"] == 0.0


def test_oscillating_decisions_raise_entropy_and_flag_leak():
    # outreach flips cut/no-cut every tick -> maximal thrash.
    ticks = [_tick("outreach"), _tick(), _tick("outreach"), _tick(), _tick("outreach")]
    rep = friction.friction_report(ticks=ticks)
    wc = rep["per_workcell"]["outreach"]
    assert wc["flips"] == 4          # flips on every transition
    assert wc["flip_rate"] == 1.0
    assert rep["decision_entropy"] == 1.0
    assert rep["energy_leak"] is True


def test_coordination_cost_is_mean_adjustments():
    ticks = [
        _tick("a", "b"),                 # 2 adjustments
        _tick("a", extra_adjust=("c",)),  # 2 adjustments
    ]
    rep = friction.friction_report(ticks=ticks)
    assert rep["coordination_cost"] == 2.0


def test_high_coordination_flags_leak():
    ticks = [_tick("a", "b", "c", "d")]  # 4 > _COORDINATION_LEAK (3.0)
    rep = friction.friction_report(ticks=ticks)
    assert rep["coordination_cost"] == 4.0
    assert rep["energy_leak"] is True
