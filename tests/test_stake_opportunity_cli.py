"""End-to-end stake_opportunity CLI flow against the fake DDB shim."""

from __future__ import annotations

from typing import Any

import pytest


class _FakeTable:
    def __init__(self):
        self.items: dict[tuple, dict[str, Any]] = {}
        self.pk_attr = None

    def put_item(self, Item):
        key_attr = self.pk_attr or next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key):
        if not Key:
            return {}
        ((k, v),) = Key.items()
        item = self.items.get((k, v))
        return {"Item": item} if item is not None else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def _patch_tables(monkeypatch):
    import backend.crm.persistence as p

    tables = {
        "_prospects_table": _FakeTable(),
        "_contacts_table": _FakeTable(),
        "_conversations_table": _FakeTable(),
        "_call_state_table": _FakeTable(),
        "_opportunities_table": _FakeTable(),
        "_operator_tasks_table": _FakeTable(),
        "_artifacts_table": _FakeTable(),
        "_onboarding_leads_table": _FakeTable(),
    }
    tables["_prospects_table"].pk_attr = "prospect_id"
    tables["_contacts_table"].pk_attr = "contact_id"
    tables["_conversations_table"].pk_attr = "conversation_id"
    tables["_call_state_table"].pk_attr = "prospect_id"
    tables["_opportunities_table"].pk_attr = "opportunity_id"
    tables["_operator_tasks_table"].pk_attr = "operator_task_id"
    tables["_artifacts_table"].pk_attr = "artifact_id"
    tables["_onboarding_leads_table"].pk_attr = "lead_id"
    for name, fake in tables.items():
        monkeypatch.setattr(p, name, lambda f=fake: f)
    return tables


def _seed_opportunity(tables, op_id="op_test", stake=""):
    row = {
        "opportunity_id": op_id,
        "prospect_id": "pr_acme",
        "stage": "new",
        "deal_size_usd": 0.0,
        "close_probability": 0.1,
        "stake_sentence": stake,
        "stake_sentence_authored_by": "",
        "stake_sentence_authored_at": "",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    tables["_opportunities_table"].items[("opportunity_id", op_id)] = row


def _redirect_budget_and_dedup(tmp_path, monkeypatch, cap=2):
    monkeypatch.setenv(
        "SAMUS_STAKE_SENTENCE_BUDGET_PATH",
        str(tmp_path / "budget.json"),
    )
    monkeypatch.setenv(
        "SAMUS_STAKE_SENTENCE_DEDUP_PATH",
        str(tmp_path / "dedup.json"),
    )
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_DAILY_CAP", str(cap))
    monkeypatch.delenv("DDB_STAKE_SENTENCE_BUDGETS_TABLE", raising=False)
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.common import stake_sentence_budget, stake_sentence_guard

    stake_sentence_budget.reset_store()
    stake_sentence_guard.reset_dedup_ledger()


_VALID = (
    "Alex picked you because your Yuba City HVAC ranks for fewer keywords "
    "than two of your neighbors combined."
)
_VALID_2 = (
    "Alex picked you because Acme Plumbing has the slowest homepage among "
    "the 12 plumbers I audited in Sutter County."
)


def test_cli_happy_path(tmp_path, monkeypatch):
    _redirect_budget_and_dedup(tmp_path, monkeypatch, cap=3)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_test")

    from backend.crm.stake_opportunity import attach_stake_sentence

    result = attach_stake_sentence(
        opportunity_id="op_test",
        stake_sentence=_VALID,
        operator_id="alex",
    )
    assert result["ok"] is True
    assert result["stake_sentence"] == _VALID
    assert result["authored_by"] == "alex"
    row = tables["_opportunities_table"].items[("opportunity_id", "op_test")]
    assert row["stake_sentence"] == _VALID
    # Artifact registered
    assert len(tables["_artifacts_table"].items) == 1


def test_cli_unknown_opportunity_rejects(tmp_path, monkeypatch):
    _redirect_budget_and_dedup(tmp_path, monkeypatch)
    _patch_tables(monkeypatch)
    from backend.crm.stake_opportunity import (
        StakeOpportunityError,
        attach_stake_sentence,
    )

    with pytest.raises(StakeOpportunityError) as exc:
        attach_stake_sentence(
            opportunity_id="op_missing",
            stake_sentence=_VALID,
            operator_id="alex",
        )
    assert exc.value.reason == "opportunity_not_found"


def test_cli_cap_exhausted(tmp_path, monkeypatch):
    _redirect_budget_and_dedup(tmp_path, monkeypatch, cap=1)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_a")
    _seed_opportunity(tables, "op_b")

    from backend.crm.stake_opportunity import (
        StakeOpportunityError,
        attach_stake_sentence,
    )

    attach_stake_sentence(
        opportunity_id="op_a",
        stake_sentence=_VALID,
        operator_id="alex",
    )
    with pytest.raises(StakeOpportunityError) as exc:
        attach_stake_sentence(
            opportunity_id="op_b",
            stake_sentence=_VALID_2,
            operator_id="alex",
        )
    assert exc.value.reason == "daily_cap_exhausted"


def test_cli_duplicate_rejects(tmp_path, monkeypatch):
    _redirect_budget_and_dedup(tmp_path, monkeypatch, cap=5)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_a")
    _seed_opportunity(tables, "op_b")

    from backend.crm.stake_opportunity import (
        StakeOpportunityError,
        attach_stake_sentence,
    )

    attach_stake_sentence(
        opportunity_id="op_a",
        stake_sentence=_VALID,
        operator_id="alex",
    )
    with pytest.raises(StakeOpportunityError) as exc:
        attach_stake_sentence(
            opportunity_id="op_b",
            stake_sentence=_VALID,
            operator_id="alex",
        )
    assert exc.value.reason == "duplicate"


def test_cli_already_staked_rejects(tmp_path, monkeypatch):
    _redirect_budget_and_dedup(tmp_path, monkeypatch, cap=5)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_a", stake=_VALID)

    from backend.crm.stake_opportunity import (
        StakeOpportunityError,
        attach_stake_sentence,
    )

    with pytest.raises(StakeOpportunityError) as exc:
        attach_stake_sentence(
            opportunity_id="op_a",
            stake_sentence=_VALID_2,
            operator_id="alex",
        )
    assert exc.value.reason == "already_staked"


def test_cli_guard_rejection_does_not_consume_budget(tmp_path, monkeypatch):
    _redirect_budget_and_dedup(tmp_path, monkeypatch, cap=2)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_a")

    from backend.common.stake_sentence_budget import get_store
    from backend.crm.stake_opportunity import (
        StakeOpportunityError,
        attach_stake_sentence,
    )

    with pytest.raises(StakeOpportunityError) as exc:
        attach_stake_sentence(
            opportunity_id="op_a",
            stake_sentence="too short",
            operator_id="alex",
        )
    assert exc.value.reason == "guard_rejected"
    # Budget untouched.
    assert get_store().count_today() == 0
