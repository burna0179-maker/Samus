"""G7 — reward computations are persisted to JSONL; write failure is
fail-CLOSED (raises :class:`RewardPersistenceError`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.strategy import reward_density as rd


class _Harm:
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
        return {"stage": "proposal", "token_cost_usd": 0.10, "prospect_id": "p"}

    def llm_cost_cents(self, _: str) -> int:
        return 10

    def stripe_payment_succeeded(self, _: str) -> bool:
        return False

    def harm_signal_store(self) -> Any:
        return _Harm()


def test_reward_persisted_to_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "reward_computations.jsonl"
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(path))

    comp1 = rd.compute_reward("op_a", store=_Store(), correlation_id="corr-1")
    comp2 = rd.compute_reward("op_b", store=_Store(), correlation_id="corr-2")

    assert path.exists(), "reward ledger file not created"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    rows = [json.loads(line) for line in lines]
    assert rows[0]["opportunity_id"] == "op_a"
    assert rows[0]["correlation_id"] == "corr-1"
    assert rows[0]["reward"] == pytest.approx(comp1.reward)
    assert rows[1]["opportunity_id"] == "op_b"
    assert rows[1]["correlation_id"] == "corr-2"
    assert rows[1]["reward"] == pytest.approx(comp2.reward)

    # Per-row components must round-trip every named term so an operator
    # replaying the ledger can reconstruct the formula.
    for required in (
        "stage_advanced",
        "stage_term",
        "llm_cost_cents",
        "llm_cost_term",
        "harm_count",
        "harm_term",
        "terminal_term",
        "raw_reward",
        "clipped_to_zero",
        "stage_weight",
        "harm_k",
    ):
        assert required in rows[0]["components"], f"missing component {required}"


def test_reward_creates_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    nested = tmp_path / "deep" / "nested" / "host_artifacts" / "reward.jsonl"
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(nested))
    rd.compute_reward("op_x", store=_Store())
    assert nested.exists()


def test_reward_persist_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write failure must raise RewardPersistenceError, never silently
    drop the audit row."""
    monkeypatch.setenv(
        "SAMUS_REWARD_PERSIST_PATH",
        str(tmp_path / "reward.jsonl"),
    )

    def _boom(_comp: rd.RewardComputation) -> None:
        raise rd.RewardPersistenceError("simulated disk-full")

    monkeypatch.setattr(rd, "_append_to_ledger", _boom)

    with pytest.raises(rd.RewardPersistenceError):
        rd.compute_reward("op_x", store=_Store())


def test_reward_persist_open_failure_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A real OSError in the write path bubbles up as RewardPersistenceError."""
    # Point the path at the tmp_path *directory itself* — opening a
    # directory for write must fail with OSError on every supported OS.
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(tmp_path))
    with pytest.raises(rd.RewardPersistenceError):
        rd.compute_reward("op_x", store=_Store())
