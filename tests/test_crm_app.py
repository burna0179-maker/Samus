"""CRM workcell FastAPI endpoint tests."""
from __future__ import annotations

from typing import Any

# Reuse the _FakeTable + _patch_tables helpers from the service test by
# duplicating their shape inline (keeping test files self-contained avoids
# import-order surprises in pytest collection).


class _FakeTable:
    def __init__(self):
        self.items: dict[tuple, dict[str, Any]] = {}
        self.fail_put = False
        self.fail_get = False
        self.pk_attr = None

    def put_item(self, Item):
        if self.fail_put:
            raise RuntimeError("simulated AWS down")
        key_attr = self.pk_attr or next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key):
        if self.fail_get:
            raise RuntimeError("simulated AWS down")
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


def _client():
    from fastapi.testclient import TestClient
    from backend.crm.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Single-entity GETs
# ---------------------------------------------------------------------------

def test_get_prospect_404_when_missing(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().get("/crm/prospects/pr_missing")
    assert r.status_code == 404
    assert "prospect_not_found" in r.text


def test_get_prospect_200_when_found(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_prospects_table"].items[("prospect_id", "pr_a")] = {
        "prospect_id": "pr_a", "company_name": "Acme", "website_url": "https://a.com",
    }
    r = _client().get("/crm/prospects/pr_a")
    assert r.status_code == 200
    body = r.json()
    assert body["prospect_id"] == "pr_a"
    assert body["company_name"] == "Acme"


def test_get_contact_404_when_missing(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().get("/crm/contacts/co_missing")
    assert r.status_code == 404


def test_get_call_state_by_prospect_id(tmp_path, monkeypatch):
    """PK is prospect_id, not call_state_id — verify the route shape."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_call_state_table"].items[("prospect_id", "pr_a")] = {
        "prospect_id": "pr_a", "state": "in_progress", "attempt_count": 1,
    }
    r = _client().get("/crm/call-state/pr_a")
    assert r.status_code == 200
    assert r.json()["state"] == "in_progress"


def test_get_operator_task_404(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().get("/crm/operator-tasks/ot_missing")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# List endpoints
# ---------------------------------------------------------------------------

def test_list_contacts_filtered(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_a")] = {
        "contact_id": "co_a", "prospect_id": "pr_acme", "name": "Alice"}
    tables["_contacts_table"].items[("contact_id", "co_b")] = {
        "contact_id": "co_b", "prospect_id": "pr_other", "name": "Bob"}
    r = _client().get("/crm/contacts?prospect_id=pr_acme")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["contacts"][0]["name"] == "Alice"


def test_list_operator_tasks_defaults_to_open(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_o")] = {
        "operator_task_id": "ot_o", "status": "open", "title": "Pending"}
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_d")] = {
        "operator_task_id": "ot_d", "status": "done", "title": "Closed out"}
    r = _client().get("/crm/operator-tasks")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["tasks"][0]["title"] == "Pending"


def test_list_opportunities_filtered_by_stage(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_n")] = {
        "opportunity_id": "op_n", "stage": "new", "name": "Acme starter"}
    tables["_opportunities_table"].items[("opportunity_id", "op_w")] = {
        "opportunity_id": "op_w", "stage": "closed_won", "name": "Beta enterprise"}
    r = _client().get("/crm/opportunities?stage=new")
    assert r.status_code == 200
    assert r.json()["count"] == 1


# ---------------------------------------------------------------------------
# POST /crm/convert/lead
# ---------------------------------------------------------------------------

def test_convert_lead_endpoint_creates(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_onboarding_leads_table"].items[("lead_id", "lead_x")] = {
        "lead_id": "lead_x", "name": "Jane", "email": "jane@acme.com",
        "company": "Acme", "website_url": "https://acme.com",
        "service_interest": ["seo_audit"], "pain_points": "x",
        "monthly_budget": "$500-$2000", "timeline": "asap",
    }
    r = _client().post("/crm/convert/lead", json={"lead_id": "lead_x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["prospect_id"].startswith("pr_")
    assert body["contact_id"].startswith("co_")


def test_convert_lead_endpoint_returns_failed_on_missing_lead(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/convert/lead", json={"lead_id": "lead_nope"})
    # 200 OK with status=failed — the workcell never raises for business errors;
    # the structured response carries the error string.
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"] == "lead_not_found"


def test_convert_lead_endpoint_rejects_empty_lead_id(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/convert/lead", json={"lead_id": ""})
    assert r.status_code == 422


def test_convert_lead_endpoint_rejects_extra_fields(tmp_path, monkeypatch):
    """extra='forbid' on the request model bubbles up as 422."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/convert/lead",
                       json={"lead_id": "lead_x", "asignee": "ops@x.com"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /work TaskEnvelope route
# ---------------------------------------------------------------------------

def test_work_envelope_routes_convert_lead(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_onboarding_leads_table"].items[("lead_id", "lead_x")] = {
        "lead_id": "lead_x", "name": "Jane", "email": "jane@acme.com",
        "company": "Acme", "website_url": "https://acme.com",
        "service_interest": [], "pain_points": "x",
    }
    envelope = {
        "task_id": "t1",
        "payload": {"lead_id": "lead_x"},
        "metadata": {"action": "convert_lead"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"


# ---------------------------------------------------------------------------
# Phase 3: POST /crm/opportunities + /crm/opportunities/{id}/advance
# ---------------------------------------------------------------------------

def test_create_opportunity_endpoint_happy(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/opportunities", json={
        "prospect_id": "pr_acme",
        "intent_score": 75,
        "monthly_budget": "$500-$2000",
        "service_interest": ["seo_audit"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["opportunity_id"].startswith("op_")


def test_create_opportunity_endpoint_rejects_missing_prospect_id(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/opportunities", json={"intent_score": 50})
    assert r.status_code == 422


def test_create_opportunity_endpoint_rejects_extra_fields(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/opportunities", json={
        "prospect_id": "pr_x", "notes": "this field is not in the schema",
    })
    assert r.status_code == 422


def _seed_op_row(tables, op_id="op_a", stage="new", **kwargs):
    row = {
        "opportunity_id": op_id,
        "prospect_id": "pr_acme",
        "stage": stage,
        "deal_size_usd": 15000.0,
        "close_probability": 0.10,
        "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
    }
    row.update(kwargs)
    tables["_opportunities_table"].items[("opportunity_id", op_id)] = row


def test_advance_opportunity_endpoint_legal(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_op_row(tables, op_id="op_a", stage="new")
    r = _client().post("/crm/opportunities/op_a/advance",
                       json={"target_stage": "qualified"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "advanced"
    assert body["prior_stage"] == "new"
    assert body["new_stage"] == "qualified"


def test_advance_opportunity_endpoint_invalid_transition(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_op_row(tables, op_id="op_b", stage="new")
    # new -> closed_won is not allowed
    r = _client().post("/crm/opportunities/op_b/advance",
                       json={"target_stage": "closed_won"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "invalid_transition"
    assert "illegal_transition" in (body["error"] or "")


def test_advance_opportunity_endpoint_not_found(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/opportunities/op_missing/advance",
                       json={"target_stage": "qualified"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "not_found"


def test_advance_opportunity_endpoint_path_id_is_authoritative(tmp_path, monkeypatch):
    """Path opportunity_id is the sole source — the route uses it verbatim."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_op_row(tables, op_id="op_path", stage="new")
    r = _client().post("/crm/opportunities/op_path/advance",
                       json={"target_stage": "qualified"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "advanced"
    assert body["opportunity_id"] == "op_path"


def test_advance_opportunity_endpoint_rejects_path_id_in_body(tmp_path, monkeypatch):
    """M3 hardening: the body input model is path-id-free (extra='forbid').

    A caller cannot smuggle ``opportunity_id`` through the body — it is now
    rejected (422) rather than silently overridden by the path id.
    """
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_op_row(tables, op_id="op_path", stage="new")
    r = _client().post("/crm/opportunities/op_path/advance",
                       json={"opportunity_id": "op_other",
                             "target_stage": "qualified"})
    assert r.status_code == 422


def test_advance_opportunity_endpoint_rejects_unknown_stage(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/opportunities/op_a/advance",
                       json={"target_stage": "frozen"})
    assert r.status_code == 422  # Literal mismatch


def test_work_envelope_routes_create_opportunity(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    envelope = {
        "task_id": "t1",
        "payload": {
            "prospect_id": "pr_acme",
            "intent_score": 95,
            "monthly_budget": "$5000+",
        },
        "metadata": {"action": "create_opportunity"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"


def test_work_envelope_routes_advance_opportunity(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_op_row(tables, op_id="op_envelope", stage="qualified")
    envelope = {
        "task_id": "t1",
        "payload": {"opportunity_id": "op_envelope", "target_stage": "proposal"},
        "metadata": {"action": "advance_opportunity"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "advanced"
    assert body["new_stage"] == "proposal"


# ---------------------------------------------------------------------------
# Phase 4: POST /crm/operator-tasks + PUT /crm/operator-tasks/{id}
# ---------------------------------------------------------------------------

def test_create_operator_task_endpoint_happy(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/operator-tasks", json={
        "kind": "review",
        "title": "Review new onboarding lead: Acme",
        "description": "From intake form",
        "related_entity_kind": "lead",
        "related_entity_id": "lead_abc",
        "source": "intake_auto",
        "source_ref": "lead_abc",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["operator_task_id"].startswith("ot_")


def test_create_operator_task_endpoint_rejects_missing_title(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/operator-tasks", json={"kind": "review"})
    assert r.status_code == 422


def test_create_operator_task_endpoint_rejects_empty_title(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/operator-tasks",
                       json={"kind": "review", "title": ""})
    assert r.status_code == 422


def test_create_operator_task_endpoint_rejects_extra_fields(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/operator-tasks", json={
        "kind": "review", "title": "x", "unexpected_field": 42,
    })
    assert r.status_code == 422


def test_create_operator_task_endpoint_rejects_unknown_kind(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/operator-tasks", json={
        "kind": "frobnicate", "title": "x",
    })
    assert r.status_code == 422


def test_update_operator_task_endpoint_happy(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1", "kind": "review", "title": "x",
        "status": "open", "description": "",
    }
    r = _client().put("/crm/operator-tasks/ot_1",
                      json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "updated"
    assert body["prior_status"] == "open"
    assert body["new_status"] == "in_progress"


def test_update_operator_task_endpoint_rejects_unknown_status(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().put("/crm/operator-tasks/ot_1",
                      json={"status": "wat"})
    assert r.status_code == 422


def test_update_operator_task_endpoint_rejects_extra_fields(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().put("/crm/operator-tasks/ot_1", json={
        "status": "in_progress", "rogue_field": 1,
    })
    assert r.status_code == 422


def test_update_operator_task_endpoint_path_id_is_authoritative(tmp_path, monkeypatch):
    """Path operator_task_id is the sole source — used verbatim by the route."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_path")] = {
        "operator_task_id": "ot_path", "kind": "review", "title": "x",
        "status": "open",
    }
    r = _client().put("/crm/operator-tasks/ot_path", json={
        "status": "in_progress",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "updated"
    assert body["operator_task_id"] == "ot_path"


def test_update_operator_task_endpoint_rejects_path_id_in_body(tmp_path, monkeypatch):
    """M3 hardening: the body input model is path-id-free (extra='forbid').

    A caller cannot smuggle ``operator_task_id`` through the body — it is now
    rejected (422) rather than silently overridden by the path id.
    """
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_path")] = {
        "operator_task_id": "ot_path", "kind": "review", "title": "x",
        "status": "open",
    }
    r = _client().put("/crm/operator-tasks/ot_path", json={
        "operator_task_id": "ot_other", "status": "in_progress",
    })
    assert r.status_code == 422


def test_update_operator_task_endpoint_not_found(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().put("/crm/operator-tasks/ot_missing",
                      json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "not_found"


def test_work_envelope_routes_create_task(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    envelope = {
        "task_id": "t1",
        "payload": {"kind": "follow_up", "title": "Ping back tomorrow"},
        "metadata": {"action": "create_task"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["operator_task_id"].startswith("ot_")


def test_work_envelope_routes_update_task(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_env")] = {
        "operator_task_id": "ot_env", "kind": "call", "title": "x",
        "status": "open",
    }
    envelope = {
        "task_id": "t1",
        "payload": {"operator_task_id": "ot_env", "status": "done"},
        "metadata": {"action": "update_task"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "updated"
    assert body["new_status"] == "done"


def test_work_envelope_unknown_action_rejected(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    envelope = {
        "task_id": "t1", "payload": {}, "metadata": {"action": "delete_world"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Phase 2 — POST /crm/conversations + POST /crm/call-state/{prospect_id}
# ---------------------------------------------------------------------------

def test_post_conversations_happy_path(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    body = {
        "conversation_id": "cv_vapi_abc",
        "prospect_id": "pr_acme",
        "channel": "call",
        "status": "completed",
        "transcript": "Hi, this is Morgan...",
        "summary": "Booked a callback",
        "source": "vapi",
        "source_ref": "call_abc",
    }
    r = _client().post("/crm/conversations", json=body)
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["persisted"] is True
    assert resp["id"] == "cv_vapi_abc"
    assert resp["error"] is None
    # Row landed in the (faked) table
    stored = tables["_conversations_table"].items[
        ("conversation_id", "cv_vapi_abc")
    ]
    assert stored["source"] == "vapi"


def test_post_conversations_rejects_invalid_body(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    # channel='carrier_pigeon' is not in the Literal — 422
    r = _client().post("/crm/conversations", json={
        "conversation_id": "cv_x", "channel": "carrier_pigeon",
    })
    assert r.status_code == 422


def test_post_conversations_rejects_missing_conversation_id(tmp_path, monkeypatch):
    """conversation_id is the PK — the route layer must reject empty values."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/conversations", json={"conversation_id": ""})
    assert r.status_code == 422


def test_post_call_state_happy_path(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    # M3 — the body input model is path-id-free; prospect_id comes from the URL.
    body = {
        "state": "completed",
        "last_call_id": "call_xyz",
        "last_outcome": "book_call",
    }
    r = _client().post("/crm/call-state/pr_acme", json=body)
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["persisted"] is True
    assert resp["id"] == "pr_acme"
    stored = tables["_call_state_table"].items[("prospect_id", "pr_acme")]
    assert stored["state"] == "completed"
    assert stored["last_call_id"] == "call_xyz"


def test_post_call_state_path_id_is_authoritative(tmp_path, monkeypatch):
    """The URL prospect_id is the sole source — used verbatim for the row PK."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    body = {"state": "completed"}
    r = _client().post("/crm/call-state/pr_correct", json=body)
    assert r.status_code == 200, r.text
    assert ("prospect_id", "pr_correct") in tables["_call_state_table"].items


def test_post_call_state_rejects_prospect_id_in_body(tmp_path, monkeypatch):
    """M3 hardening: the body input model is path-id-free (extra='forbid').

    A caller cannot smuggle ``prospect_id`` through the body — it is now
    rejected (422) rather than silently overridden by the path id.
    """
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/call-state/pr_correct",
                       json={"prospect_id": "pr_wrong", "state": "completed"})
    assert r.status_code == 422


def test_post_call_state_rejects_invalid_state(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/call-state/pr_x", json={"state": "loitering"})
    assert r.status_code == 422


def test_work_envelope_routes_upsert_conversation(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    envelope = {
        "task_id": "t_conv",
        "payload": {
            "conversation_id": "cv_envelope",
            "channel": "call",
            "status": "completed",
            "source": "vapi",
        },
        "metadata": {"action": "upsert_conversation"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persisted"] is True
    assert body["id"] == "cv_envelope"
    assert ("conversation_id", "cv_envelope") in tables["_conversations_table"].items


def test_work_envelope_routes_upsert_call_state(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    envelope = {
        "task_id": "t_cs",
        "payload": {"prospect_id": "pr_envelope", "state": "completed"},
        "metadata": {"action": "upsert_call_state"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persisted"] is True
    assert body["id"] == "pr_envelope"
    assert ("prospect_id", "pr_envelope") in tables["_call_state_table"].items


# ---------------------------------------------------------------------------
# Phase 5 — POST /crm/artifacts + GET /crm/_find_opportunity_for_email
# ---------------------------------------------------------------------------

def test_post_artifacts_happy(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    r = _client().post("/crm/artifacts", json={
        "kind": "seo_audit",
        "owner_entity_kind": "prospect",
        "owner_entity_id": "pr_acme",
        "title": "SEO audit acme.com",
        "inline_data": {"seo_score": 80},
        "source": "seo",
        "created_by": "samus-seo",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["artifact_id"].startswith("ar_")
    assert len(tables["_artifacts_table"].items) == 1


def test_post_artifacts_rejects_missing_owner(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    # owner_entity_kind/id are min_length=1 -> 422
    r = _client().post("/crm/artifacts", json={
        "kind": "proposal",
        "owner_entity_kind": "",
        "owner_entity_id": "",
    })
    assert r.status_code == 422


def test_post_artifacts_rejects_unknown_kind(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/artifacts", json={
        "kind": "doomsday_device",
        "owner_entity_kind": "prospect",
        "owner_entity_id": "pr_x",
    })
    assert r.status_code == 422


def test_post_artifacts_rejects_extra_fields(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().post("/crm/artifacts", json={
        "kind": "proposal",
        "owner_entity_kind": "opportunity",
        "owner_entity_id": "op_x",
        "rogue": "ride",
    })
    assert r.status_code == 422


def test_find_opportunity_for_email_endpoint_returns_id(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_a")] = {
        "contact_id": "co_a", "prospect_id": "pr_a",
        "email": "buyer@x.com",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_open")] = {
        "opportunity_id": "op_open", "prospect_id": "pr_a",
        "stage": "negotiation",
        "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
    }
    r = _client().get("/crm/_find_opportunity_for_email?email=buyer@x.com")
    assert r.status_code == 200, r.text
    assert r.json()["opportunity_id"] == "op_open"


def test_find_opportunity_for_email_endpoint_returns_null_on_miss(
    tmp_path, monkeypatch,
):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    r = _client().get("/crm/_find_opportunity_for_email?email=nobody@x.com")
    assert r.status_code == 200
    assert r.json()["opportunity_id"] is None


def test_work_envelope_routes_create_artifact(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    envelope = {
        "task_id": "t_art",
        "payload": {
            "kind": "proposal",
            "owner_entity_kind": "opportunity",
            "owner_entity_id": "op_z",
            "title": "Proposal: Acme",
            "source": "proposal",
            "created_by": "samus-proposal",
        },
        "metadata": {"action": "create_artifact"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["artifact_id"].startswith("ar_")
    assert len(tables["_artifacts_table"].items) == 1


def test_work_envelope_routes_find_opportunity_for_email(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_a")] = {
        "contact_id": "co_a", "prospect_id": "pr_a",
        "email": "envelope_buyer@x.com",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_env_open")] = {
        "opportunity_id": "op_env_open", "prospect_id": "pr_a",
        "stage": "qualified",
    }
    envelope = {
        "task_id": "t_lookup",
        "payload": {"email": "envelope_buyer@x.com"},
        "metadata": {"action": "find_opportunity_for_email"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    assert r.json()["opportunity_id"] == "op_env_open"


def test_work_envelope_routes_close_opportunity_from_payment(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_pay")] = {
        "opportunity_id": "op_pay", "stage": "proposal",
        "deal_size_usd": 999.0,
    }
    envelope = {
        "task_id": "evt_stripe_42",
        "payload": {
            "opportunity_id": "op_pay",
            "won_amount_usd": 999.0,  # under FIN-10 cap; test is about routing
            "payment_ref": "evt_stripe_42",
        },
        "metadata": {"action": "close_opportunity_from_payment"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "advanced"
    assert body["new_stage"] == "closed_won"
