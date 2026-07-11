"""G7 — negative reward inputs clip to zero (Codex chapter 04 G7 spec)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.strategy import reward_density as rd


class _HarmStub:
    def __init__(self, retracted=0, unsubs=0, complaints=0):
        self.r, self.u, self.c = retracted, unsubs, complaints

    def artifacts_for_opportunity(self, _: str) -> list[dict[str, Any]]:
        return [{"kind": "retracted"} for _ in range(self.r)]

    def conversations_for_prospect(self, _: str) -> list[dict[str, Any]]:
        return [{"outcome": "unsubscribe"} for _ in range(self.u)]

    def contacts_for_prospect(self, _: str) -> list[dict[str, Any]]:
        return [{"email": f"c{i}@x"} for i in range(self.c)]

    def opportunity(self, _: str) -> dict[str, Any]:
        return {"prospect_id": "p"}

    def complaint_recipients(self) -> list[str]:
        return [f"c{i}@x" for i in range(self.c)]


class _Store:
    def __init__(self, stage, cost_usd, harm, stripe=False):
        self._opp = {"stage": stage, "token_cost_usd": cost_usd, "prospect_id": "p"}
        self._cents = int(round(cost_usd * 100))
        self._stripe = stripe
        self._harm = _HarmStub(complaints=harm)

    def opportunity(self, _: str):
        return dict(self._opp)

    def llm_cost_cents(self, _: str) -> int:
        return self._cents

    def stripe_payment_succeeded(self, _: str) -> bool:
        return self._stripe

    def harm_signal_store(self):
        return self._harm


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "SAMUS_REWARD_PERSIST_PATH", str(tmp_path / "reward.jsonl"),
    )


def test_negative_reward_clips_to_zero():
    # stage_advanced=0 ("new"), cost=$10 -> 1000c * 0.01 = 10 penalty
    # raw = 0 - 10 - 0 + 0 = -10 -> clipped to 0
    store = _Store(stage="new", cost_usd=10.0, harm=0)
    comp = rd.compute_reward("op_x", store=store)
    assert comp.reward == 0.0
    assert comp.components["raw_reward"] == pytest.approx(-10.0)
    assert comp.components["clipped_to_zero"] == 1.0


def test_overwhelming_harm_clips_to_zero():
    # stage 1 ("qualified") -> stage_term 1
    # harm 10 complaints -> 10 * 5 = 50
    # raw = 1 - 0 - 50 + 0 = -49 -> 0
    store = _Store(stage="qualified", cost_usd=0.0, harm=10)
    comp = rd.compute_reward("op_x", store=store)
    assert comp.reward == 0.0
    assert comp.components["harm_term"] == pytest.approx(50.0)
    assert comp.components["clipped_to_zero"] == 1.0


def test_zero_inputs_yield_zero_reward_not_negative():
    store = _Store(stage="new", cost_usd=0.0, harm=0)
    comp = rd.compute_reward("op_x", store=store)
    assert comp.reward == 0.0
    # raw exactly 0 — not flagged as clipped (raw_reward > 0 is the flag).
    assert comp.components["raw_reward"] == 0.0
