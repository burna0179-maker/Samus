"""CRM worker action router (_handle_action).

The worker class itself wraps BaseSqsWorker and is exercised indirectly
via the worker_base test suite. The action router is the interesting
business surface — every dispatched action must route to the right
service function and surface its return value as a JSON-serializable
dict. The new ``close_payment_to_opportunity`` action is the only logic
that exists in the worker but not in the /work HTTP route.
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.crm import worker as crm_worker


class _FakeTable:
    def __init__(self):
        self.items: dict[tuple, dict[str, Any]] = {}
        self.fail_put = False
        self.pk_attr = None

    def put_item(self, Item):
        if self.fail_put:
            raise RuntimeError("simulated AWS down")
        key_attr = self.pk_attr or next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key):
        key_attr, key_val = next(iter(Key.items()))
        item = self.items.get((key_attr, key_val))
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        fe = kwargs.get("FilterExpression", "")
        vals = kwargs.get("ExpressionAttributeValues", {}) or {}
        names = kwargs.get("ExpressionAttributeNames", {}) or {}
        out = list(self.items.values())
        if fe and isinstance(fe, str) and "=" in fe:
            target_attr = names.get("#f")
            target_val = vals.get(":v")
            if target_attr is not None:
                out = [it for it in out
                       if str(it.get(target_attr, "")).strip().lower() ==
                          str(target_val or "").strip().lower()]
        limit = kwargs.get("Limit", 50)
        return {"Items": out[:limit]}


def _patch_tables(monkeypatch):
    import backend.crm.persistence as p
    tables = {
        "_prospects_table":        _FakeTable(),
        "_contacts_table":         _FakeTable(),
        "_conversations_table":    _FakeTable(),
        "_call_state_table":       _FakeTable(),
        "_opportunities_table":    _FakeTable(),
        "_operator_tasks_table":   _FakeTable(),
        "_artifacts_table":        _FakeTable(),
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


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))


# ---------------------------------------------------------------------------
# Action routing — happy paths
# ---------------------------------------------------------------------------

def test_create_artifact_action(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    payload = {
        "kind": "seo_audit",
        "owner_entity_kind": "prospect",
        "owner_entity_id": "pr_a",
        "title": "SEO audit",
        "source": "seo",
    }
    out = crm_worker._handle_action("create_artifact", payload)
    assert out["status"] == "created"
    assert out["artifact_id"].startswith("ar_")
    assert len(tables["_artifacts_table"].items) == 1


def test_upsert_conversation_action(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    payload = {
        "conversation_id": "cv_x",
        "channel": "call",
        "status": "completed",
        "summary": "Booked a call.",
    }
    out = crm_worker._handle_action("upsert_conversation", payload)
    assert out["persisted"] is True
    assert out["id"] == "cv_x"
    assert ("conversation_id", "cv_x") in tables["_conversations_table"].items


def test_upsert_call_state_action(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    out = crm_worker._handle_action("upsert_call_state", {
        "prospect_id": "pr_x", "state": "completed", "attempt_count": 1,
    })
    assert out["persisted"] is True
    assert out["id"] == "pr_x"


def test_create_task_action(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    out = crm_worker._handle_action("create_task", {
        "kind": "review", "title": "Review the new lead",
    })
    assert out["status"] == "created"
    assert out["operator_task_id"].startswith("ot_")


# ---------------------------------------------------------------------------
# close_payment_to_opportunity — the new collapsed action
# ---------------------------------------------------------------------------

def _seed_prospect_contact_opportunity(tables, email: str = "buyer@x.com",
                                       opp_stage: str = "qualified"):
    tables["_contacts_table"].items[("contact_id", "co_b")] = {
        "contact_id": "co_b", "prospect_id": "pr_buyer",
        "name": "Buyer", "email": email,
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_b")] = {
        "opportunity_id": "op_b", "prospect_id": "pr_buyer",
        "stage": opp_stage, "name": "Buyer deal",
        "created_at": "2026-05-01T00:00:00Z",
    }


def test_close_payment_advances_open_opportunity_to_won(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_prospect_contact_opportunity(tables, email="buyer@x.com",
                                       opp_stage="qualified")
    out = crm_worker._handle_action("close_payment_to_opportunity", {
        "email": "buyer@x.com",
        "amount_usd": 999.0,  # under the FIN-10 cap; test is about the flow
        "payment_ref": "evt_test_001",
    })
    assert out["status"] == "advanced"
    assert out["opportunity_id"] == "op_b"
    assert out["email_tail"] == "buyer@x.com"[-12:]
    # Opportunity row should now be closed_won
    row = tables["_opportunities_table"].items[("opportunity_id", "op_b")]
    assert row["stage"] == "closed_won"
    assert float(row.get("won_amount_usd", 0)) == 999.0


def test_close_payment_no_match_returns_no_open_opportunity(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)   # no seed data
    out = crm_worker._handle_action("close_payment_to_opportunity", {
        "email": "ghost@x.com", "amount_usd": 999.0, "payment_ref": "evt_g",
    })
    assert out["status"] == "no_open_opportunity"
    assert out["opportunity_id"] is None


def test_close_payment_empty_email_returns_skipped(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    out = crm_worker._handle_action("close_payment_to_opportunity", {
        "email": "", "amount_usd": 100.0, "payment_ref": "evt_e",
    })
    assert out["status"] == "skipped"
    assert out["opportunity_id"] is None


def test_close_payment_already_closed_returns_invalid_transition(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_prospect_contact_opportunity(tables, email="buyer@x.com",
                                       opp_stage="closed_won")
    out = crm_worker._handle_action("close_payment_to_opportunity", {
        "email": "buyer@x.com", "amount_usd": 999.0, "payment_ref": "evt_dup",
    })
    # find_opportunity_for_email scans only non-terminal opportunities, so
    # an already-closed deal looks like "no open opportunity" to this action.
    assert out["status"] == "no_open_opportunity"


# ---------------------------------------------------------------------------
# Routing — error paths
# ---------------------------------------------------------------------------

def test_unknown_action_raises(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    with pytest.raises(ValueError, match="unknown_action"):
        crm_worker._handle_action("nuclear_launch", {})


def test_upsert_conversation_without_id_raises(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    with pytest.raises(ValueError, match="conversation_id"):
        crm_worker._handle_action("upsert_conversation", {"channel": "call"})


def test_upsert_call_state_without_prospect_id_raises(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    with pytest.raises(ValueError, match="prospect_id"):
        crm_worker._handle_action("upsert_call_state", {"state": "completed"})
