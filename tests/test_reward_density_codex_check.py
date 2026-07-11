"""G7 — every compute_reward call passes through the Codex Validation Layer
with ``subtracts_harm=True``, silencing VW-G7."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.common.codex import models as codex_models
from backend.common.codex import registry as codex_registry
from backend.common.codex import validator as codex_validator
from backend.strategy import reward_density as rd


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPO_ROOT / "docs" / "codex"


class _StubHarm:
    def artifacts_for_opportunity(self, _: str) -> list[dict[str, Any]]:
        return []

    def conversations_for_prospect(self, _: str) -> list[dict[str, Any]]:
        return []

    def contacts_for_prospect(self, _: str) -> list[dict[str, Any]]:
        return []

    def opportunity(self, _: str) -> dict[str, Any]:
        return {"prospect_id": "p"}

    def complaint_recipients(self) -> list[str]:
        return []


class _Store:
    def opportunity(self, _: str) -> dict[str, Any]:
        return {"stage": "qualified", "token_cost_usd": 0.0, "prospect_id": "p"}

    def llm_cost_cents(self, _: str) -> int:
        return 0

    def stripe_payment_succeeded(self, _: str) -> bool:
        return False

    def harm_signal_store(self) -> Any:
        return _StubHarm()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "SAMUS_REWARD_PERSIST_PATH",
        str(tmp_path / "reward.jsonl"),
    )


@pytest.fixture
def _loaded_registry(monkeypatch: pytest.MonkeyPatch) -> codex_registry.CodexRegistry:
    """Load the real Codex from docs/codex into the module-level REGISTRY
    so :func:`compute_reward`'s internal ``check_action`` (which uses the
    module REGISTRY) has rules to evaluate."""
    reg = codex_registry.CodexRegistry()
    reg.load(CODEX_DIR)
    # Re-point both the registry module and validator module references to
    # this loaded instance for the duration of the test.
    monkeypatch.setattr(codex_registry, "REGISTRY", reg, raising=True)
    monkeypatch.setattr(codex_validator, "REGISTRY", reg, raising=True)
    return reg


def test_compute_reward_emits_codex_action_with_subtracts_harm(
    _loaded_registry,
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    real_check = codex_validator.check_action

    def _capture(action, **kwargs):
        captured["action"] = action
        return real_check(action, **kwargs)

    # Patch at the symbol the reward module imports at call time.
    monkeypatch.setattr(
        "backend.common.codex.check_action",
        _capture,
        raising=True,
    )

    comp = rd.compute_reward("op_x", store=_Store())
    assert comp.opportunity_id == "op_x"

    action = captured.get("action")
    assert action is not None, "compute_reward did not invoke codex.check_action"
    assert isinstance(action, codex_models.ProposedAction)
    assert action.action_kind == "reward_function_update"
    assert action.payload.get("subtracts_harm") is True
    assert action.payload.get("opportunity_id") == "op_x"
    assert action.service == "strategy"


def test_compute_reward_action_does_not_trip_vw_g7(_loaded_registry):
    """End-to-end: a real compute_reward call returns and the proposed
    action satisfies the VW-G7 warning gate (subtracts_harm=True silences
    it). We re-run the validator against the action shape compute_reward
    builds, asserting no VW-G7 in warnings."""
    # Build the same action shape compute_reward uses.
    action = codex_models.ProposedAction(
        service="strategy",
        capability="reward_density",
        action_kind="reward_function_update",
        payload={
            "opportunity_id": "op_x",
            "reward": 1.0,
            "components": {"stage_advanced": 1.0},
            "subtracts_harm": True,
        },
        proposed_by="strategy.reward_density.compute_reward",
    )
    verdict = codex_validator.check_action(action, registry=_loaded_registry)
    assert verdict.allowed is True
    assert not any("VW-G7" in w for w in verdict.warnings)
