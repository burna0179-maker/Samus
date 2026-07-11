"""G7 — ADR-004 reward formula math.

Feeds synthetic stage / cost / harm / stripe values through
:func:`backend.strategy.reward_density.compute_reward` and asserts the
per-term components match the spec.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.strategy import reward_density as rd


class _StubHarmStore:
    def __init__(self, retracted: int = 0, unsubs: int = 0, complaints: int = 0):
        self.retracted = retracted
        self.unsubs = unsubs
        self.complaints = complaints

    def artifacts_for_opportunity(self, opportunity_id: str) -> list[dict[str, Any]]:
        return [{"kind": "retracted_claim", "title": ""} for _ in range(self.retracted)]

    def conversations_for_prospect(self, prospect_id: str) -> list[dict[str, Any]]:
        return [{"outcome": "unsubscribe"} for _ in range(self.unsubs)]

    def contacts_for_prospect(self, prospect_id: str) -> list[dict[str, Any]]:
        return [{"email": f"c{i}@example.com"} for i in range(self.complaints)]

    def opportunity(self, opportunity_id: str) -> dict[str, Any]:
        return {"prospect_id": f"p_{opportunity_id}"}

    def complaint_recipients(self) -> list[str]:
        return [f"c{i}@example.com" for i in range(self.complaints)]


class _StubStore:
    def __init__(
        self, *, stage: str, llm_cost_usd: float,
        retracted: int = 0, unsubs: int = 0, complaints: int = 0,
        stripe_paid: bool = False,
    ):
        self._opp = {
            "opportunity_id": "op_test",
            "prospect_id": "p_test",
            "stage": stage,
            "token_cost_usd": llm_cost_usd,
        }
        self._llm_cost_cents = int(round(llm_cost_usd * 100))
        self._stripe = stripe_paid
        self._harm = _StubHarmStore(retracted, unsubs, complaints)

    def opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        return dict(self._opp)

    def llm_cost_cents(self, opportunity_id: str) -> int:
        return self._llm_cost_cents

    def stripe_payment_succeeded(self, opportunity_id: str) -> bool:
        return self._stripe

    def harm_signal_store(self) -> Any:
        return self._harm


@pytest.fixture(autouse=True)
def _isolate_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route the audit ledger to a per-test tmp path so the formula tests
    never touch the production /opt/samus/data path."""
    p = tmp_path / "reward_computations.jsonl"
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(p))
    # Pin coefficients to spec defaults so an env-var leak from another
    # test process can't perturb the math.
    monkeypatch.delenv("SAMUS_REWARD_STAGE_WEIGHT", raising=False)
    monkeypatch.delenv("SAMUS_REWARD_LLM_COST_WEIGHT", raising=False)
    monkeypatch.delenv("SAMUS_REWARD_HARM_K", raising=False)
    monkeypatch.delenv("SAMUS_REWARD_TERMINAL_MULTIPLIER", raising=False)
    return p


def test_compute_reward_proposal_stage_no_harm_no_stripe(_isolate_persist):
    # stage_advanced("proposal") == 2 (new->qualified->proposal)
    # llm_cost = $0.50 -> 50 cents -> 50 * 0.01 = 0.5
    # harm = 0
    # terminal = 0
    # reward = 2.0 - 0.5 - 0 + 0 = 1.5
    store = _StubStore(stage="proposal", llm_cost_usd=0.50)
    comp = rd.compute_reward("op_test", store=store)
    assert comp.opportunity_id == "op_test"
    assert comp.reward == pytest.approx(1.5)
    assert comp.components["stage_advanced"] == 2.0
    assert comp.components["stage_term"] == pytest.approx(2.0)
    assert comp.components["llm_cost_cents"] == 50.0
    assert comp.components["llm_cost_term"] == pytest.approx(0.5)
    assert comp.components["harm_term"] == 0.0
    assert comp.components["terminal_term"] == 0.0
    assert comp.components["clipped_to_zero"] == 0.0


def test_compute_reward_closed_won_with_stripe(_isolate_persist):
    # stage_advanced("closed_won") == 4
    # llm_cost = $1.00 -> 100c * 0.01 = 1.0
    # harm = 0
    # terminal = 100
    # reward = 4 - 1 - 0 + 100 = 103
    store = _StubStore(
        stage="closed_won", llm_cost_usd=1.00, stripe_paid=True,
    )
    comp = rd.compute_reward("op_test", store=store)
    assert comp.reward == pytest.approx(103.0)
    assert comp.components["terminal_paid"] == 1.0
    assert comp.components["terminal_term"] == pytest.approx(100.0)


def test_compute_reward_subtracts_each_harm_type(_isolate_persist):
    # stage 1 (qualified), no cost, 1 retracted + 2 unsubs + 1 complaint = 4
    # harm_term = 4 * 5 = 20
    # reward raw = 1 - 0 - 20 + 0 = -19 -> clipped to 0
    store = _StubStore(
        stage="qualified", llm_cost_usd=0.0,
        retracted=1, unsubs=2, complaints=1,
    )
    comp = rd.compute_reward("op_test", store=store)
    assert comp.components["retracted_claims"] == 1.0
    assert comp.components["unsubscribes"] == 2.0
    assert comp.components["complaints"] == 1.0
    assert comp.components["harm_count"] == 4.0
    assert comp.components["harm_term"] == pytest.approx(20.0)


def test_compute_reward_closed_won_retainer_scores_same_as_closed_won(_isolate_persist):
    """closed_won_retainer should not out-score a straight closed_won."""
    store_won = _StubStore(stage="closed_won", llm_cost_usd=0.0)
    store_retainer = _StubStore(stage="closed_won_retainer", llm_cost_usd=0.0)
    a = rd.compute_reward("op_test", store=store_won)
    b = rd.compute_reward("op_test", store=store_retainer)
    assert a.components["stage_advanced"] == b.components["stage_advanced"]


def test_compute_reward_env_coefficient_override(_isolate_persist, monkeypatch):
    monkeypatch.setenv("SAMUS_REWARD_STAGE_WEIGHT", "2.0")
    # proposal -> stage_advanced=2, stage_term = 2 * 2 = 4
    store = _StubStore(stage="proposal", llm_cost_usd=0.0)
    comp = rd.compute_reward("op_test", store=store)
    assert comp.components["stage_weight"] == pytest.approx(2.0)
    assert comp.components["stage_term"] == pytest.approx(4.0)


def test_compute_reward_missing_opportunity_raises(_isolate_persist):
    class _Empty:
        def opportunity(self, _id: str):
            return None
        def llm_cost_cents(self, _id: str) -> int:
            return 0
        def stripe_payment_succeeded(self, _id: str) -> bool:
            return False
        def harm_signal_store(self) -> Any:
            return _StubHarmStore()

    with pytest.raises(ValueError, match="opportunity not found"):
        rd.compute_reward("op_missing", store=_Empty())
