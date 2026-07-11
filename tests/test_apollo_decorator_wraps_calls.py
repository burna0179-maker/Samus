"""@apollo_budgeted — under cap returns, over cap raises and the call doesn't run."""

from __future__ import annotations

import pytest

from backend.common import apollo_budget
from backend.common.apollo_budget import (
    ApolloBudgetExceeded,
    ApolloBudgetStore,
    apollo_budgeted,
)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Replace the module-level singleton with a JSON-only test store."""
    apollo_budget.reset_store()
    store = ApolloBudgetStore(
        ddb_table=None,
        json_path=str(tmp_path / "apollo.json"),
        daily_cap_usd=lambda: 0.20,  # cheap cap so we can blow through it
    )
    monkeypatch.setattr(apollo_budget, "_STORE", store)
    yield store
    apollo_budget.reset_store()


def test_decorator_under_cap_runs_and_records(isolated_store):
    calls: list[str] = []

    @apollo_budgeted("people_search")
    def synthetic_apollo_call(query: str) -> str:
        calls.append(query)
        return f"ok:{query}"

    result = synthetic_apollo_call("owner")
    assert result == "ok:owner"
    assert calls == ["owner"]
    # Spend recorded: people_search default = $0.04.
    assert isolated_store.current_spend_usd() == pytest.approx(0.04)


def test_decorator_over_cap_raises_and_no_call(isolated_store):
    calls: list[str] = []

    @apollo_budgeted("phone_unlock")  # 8 credits * 0.04 = 0.32 > cap 0.20
    def synthetic_apollo_call(query: str) -> str:
        calls.append(query)
        return f"ok:{query}"

    with pytest.raises(ApolloBudgetExceeded):
        synthetic_apollo_call("owner")

    # Critical: the wrapped function MUST NOT have been called.
    assert calls == []
    # And no spend recorded.
    assert isolated_store.current_spend_usd() == 0.0


def test_decorator_prospect_id_kwarg_flows_to_ledger(isolated_store):
    @apollo_budgeted("email_unlock", prospect_id_kwarg="contact_id")
    def unlock(*, contact_id: str) -> str:
        return f"unlocked:{contact_id}"

    unlock(contact_id="apollo_person_42")
    snap = isolated_store.snapshot()
    assert snap.recent_calls[-1].endpoint == "email_unlock"
    assert snap.recent_calls[-1].prospect_id == "apollo_person_42"


def test_decorator_accumulates_across_calls(isolated_store):
    @apollo_budgeted("people_search")  # 0.04 each
    def search() -> str:
        return "ok"

    # 5 calls at 0.04 = 0.20, exactly the cap. Sixth should fail.
    for _ in range(5):
        search()
    assert isolated_store.current_spend_usd() == pytest.approx(0.20)
    with pytest.raises(ApolloBudgetExceeded):
        search()
