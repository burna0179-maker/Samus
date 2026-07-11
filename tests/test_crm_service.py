"""CRM service — reads + lead conversion."""

from __future__ import annotations

import json
from typing import Any


class _FakeTable:
    """In-memory DDB shim. Supports get_item, put_item, scan with
    minimal FilterExpression handling (just equality on a single attr)."""

    def __init__(self):
        self.items: dict[tuple, dict[str, Any]] = {}
        self.fail_put = False
        self.fail_get = False
        self.pk_attr = None  # set per-table via _patch_tables

    def put_item(self, Item):
        if self.fail_put:
            raise RuntimeError("simulated AWS down")
        # Composite key for our shim — single-PK only, but we tolerate any
        # PK field name. Caller sets pk_attr; if unset we infer from Item.
        key_attr = self.pk_attr or next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key):
        if self.fail_get:
            raise RuntimeError("simulated AWS down")
        if not Key:
            return {}
        key_attr, key_val = next(iter(Key.items()))
        item = self.items.get((key_attr, key_val))
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        fe = kwargs.get("FilterExpression", "")
        vals = kwargs.get("ExpressionAttributeValues", {}) or {}
        names = kwargs.get("ExpressionAttributeNames", {}) or {}
        out = list(self.items.values())
        # Naive parse: "#f = :v" -> filter where item[names['#f']] == vals[':v']
        if fe and isinstance(fe, str) and "=" in fe:
            target_attr = names.get("#f")
            target_val = vals.get(":v")
            if target_attr is not None:
                out = [
                    it
                    for it in out
                    if str(it.get(target_attr, "")).strip().lower()
                    == str(target_val or "").strip().lower()
                ]
        limit = kwargs.get("Limit", 50)
        return {"Items": out[:limit]}


def _patch_tables(monkeypatch):
    """Replace all CRM table accessors with fresh _FakeTable shims."""
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


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))


def _disable_close_cap(monkeypatch):
    """Disable the FIN-10 closed-won amount cap for tests whose intent is the
    won-amount side effect / default-fallback logic at a realistic large deal
    size (the cap is active by default at $1000). Mirrors an operator who
    explicitly wants no ceiling (SAMUS_CRM_MAX_CLOSE_AMOUNT_USD <= 0)."""
    from backend.common.settings import reload_settings

    monkeypatch.setenv("SAMUS_CRM_MAX_CLOSE_AMOUNT_USD", "0")
    reload_settings()


def _seed_lead(tables, lead_id="lead_x"):
    tables["_onboarding_leads_table"].items[("lead_id", lead_id)] = {
        "lead_id": lead_id,
        "name": "Jane Smith",
        "email": "jane@acme.com",
        "company": "Acme Roofing",
        "website_url": "https://acmeroofing.com",
        "service_interest": ["seo_audit"],
        "pain_points": "Manual outreach is killing us.",
        "monthly_budget": "$500-$2000",
        "timeline": "this_month",
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_get_prospect_returns_none_when_missing(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.service import get_prospect

    assert get_prospect("pr_does_not_exist") is None


def test_get_contact_returns_typed_row(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_a")] = {
        "contact_id": "co_a",
        "name": "Alice",
        "email": "alice@x.com",
        "preferred_channel": "phone",
    }
    from backend.crm.service import get_contact

    c = get_contact("co_a")
    assert c is not None
    assert c.name == "Alice"
    assert c.preferred_channel == "phone"


def test_list_contacts_filtered_by_prospect_id(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_a")] = {
        "contact_id": "co_a",
        "prospect_id": "pr_acme",
        "name": "Alice",
    }
    tables["_contacts_table"].items[("contact_id", "co_b")] = {
        "contact_id": "co_b",
        "prospect_id": "pr_acme",
        "name": "Bob",
    }
    tables["_contacts_table"].items[("contact_id", "co_c")] = {
        "contact_id": "co_c",
        "prospect_id": "pr_other",
        "name": "Carol",
    }
    from backend.crm.service import list_contacts

    out = list_contacts(prospect_id="pr_acme")
    assert out.count == 2
    assert {c.name for c in out.contacts} == {"Alice", "Bob"}


def test_list_operator_tasks_filtered_by_status_open(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1",
        "status": "open",
        "title": "Call back",
    }
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_2")] = {
        "operator_task_id": "ot_2",
        "status": "done",
        "title": "Sent contract",
    }
    from backend.crm.service import list_operator_tasks

    out = list_operator_tasks(status="open")
    assert out.count == 1
    assert out.tasks[0].title == "Call back"


# ---------------------------------------------------------------------------
# Conversion: lead -> Prospect + Contact
# ---------------------------------------------------------------------------


def test_convert_lead_creates_prospect_and_contact(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_lead(tables)

    from backend.crm.service import convert_lead_to_prospect
    from backend.crm.models import ConvertLeadRequest

    result = convert_lead_to_prospect(ConvertLeadRequest(lead_id="lead_x"))

    assert result.status == "created"
    assert result.prospect_id.startswith("pr_")
    assert result.contact_id.startswith("co_")
    assert result.error is None
    assert len(tables["_prospects_table"].items) == 1
    assert len(tables["_contacts_table"].items) == 1


def test_convert_lead_idempotent_when_contact_email_exists(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_lead(tables)
    # Pre-seed an existing contact with the same email
    tables["_contacts_table"].items[("contact_id", "co_existing")] = {
        "contact_id": "co_existing",
        "prospect_id": "pr_existing",
        "name": "Jane Smith",
        "email": "jane@acme.com",
    }

    from backend.crm.service import convert_lead_to_prospect
    from backend.crm.models import ConvertLeadRequest

    result = convert_lead_to_prospect(ConvertLeadRequest(lead_id="lead_x"))

    assert result.status == "existing"
    assert result.contact_id == "co_existing"
    assert result.prospect_id == "pr_existing"
    # No new rows written
    assert len(tables["_contacts_table"].items) == 1
    assert len(tables["_prospects_table"].items) == 0


def test_convert_lead_attaches_to_existing_prospect_by_website(tmp_path, monkeypatch):
    """Different email but same company website -> reuse Prospect, add new Contact."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_lead(tables)
    # Pre-seed an existing prospect with the same website
    tables["_prospects_table"].items[("prospect_id", "pr_existing_acme")] = {
        "prospect_id": "pr_existing_acme",
        "company_name": "Acme Roofing",
        "website_url": "https://acmeroofing.com",
    }

    from backend.crm.service import convert_lead_to_prospect
    from backend.crm.models import ConvertLeadRequest

    result = convert_lead_to_prospect(ConvertLeadRequest(lead_id="lead_x"))

    assert result.status == "created"
    assert result.prospect_id == "pr_existing_acme"  # reused
    assert result.contact_id.startswith("co_")  # new
    assert len(tables["_prospects_table"].items) == 1  # no new prospect
    assert len(tables["_contacts_table"].items) == 1  # new contact


def test_convert_lead_fails_when_lead_not_found(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)  # no lead seeded
    from backend.crm.service import convert_lead_to_prospect
    from backend.crm.models import ConvertLeadRequest

    result = convert_lead_to_prospect(ConvertLeadRequest(lead_id="lead_missing"))
    assert result.status == "failed"
    assert result.error == "lead_not_found"


def test_convert_lead_fails_when_ddb_put_fails(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_lead(tables)
    tables["_prospects_table"].fail_put = True
    from backend.crm.service import convert_lead_to_prospect
    from backend.crm.models import ConvertLeadRequest

    result = convert_lead_to_prospect(ConvertLeadRequest(lead_id="lead_x"))
    assert result.status == "failed"
    assert "ddb_put_failed" in (result.error or "")


# ---------------------------------------------------------------------------
# Phase 3: create_opportunity + advance_opportunity
# ---------------------------------------------------------------------------


def test_create_opportunity_happy_path_with_scoring(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)

    from backend.crm.service import create_opportunity, get_opportunity
    from backend.crm.models import CreateOpportunityRequest

    req = CreateOpportunityRequest(
        prospect_id="pr_acme",
        contact_id="co_jane",
        name="Acme starter",
        intent_score=85,
        monthly_budget="$500-$2000",
        service_interest=["seo_audit"],
        assigned_to="ops@hf.tech",
    )
    result = create_opportunity(req)

    assert result.status == "created"
    assert result.opportunity_id.startswith("op_")
    assert result.error is None
    # Persisted to DDB
    assert len(tables["_opportunities_table"].items) == 1
    op = get_opportunity(result.opportunity_id)
    assert op is not None
    assert op.stage == "new"
    assert op.prospect_id == "pr_acme"
    # Hot tier (intent_score=85) -> 0.35 close prob baseline
    assert op.close_probability == 0.35
    # $500-$2000 midpoint=1250 * 12 = 15000 (no multi-service bump)
    assert op.deal_size_usd == 15000.0
    assert "tier=hot" in op.next_step


def test_create_opportunity_without_signals_uses_stage_baseline(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)

    from backend.crm.service import create_opportunity, get_opportunity
    from backend.crm.models import CreateOpportunityRequest

    req = CreateOpportunityRequest(prospect_id="pr_x")
    result = create_opportunity(req)

    assert result.status == "created"
    op = get_opportunity(result.opportunity_id)
    assert op is not None
    assert op.stage == "new"
    assert op.close_probability == 0.10  # STAGE_PROBABILITIES["new"]
    assert op.deal_size_usd == 0.0


def test_create_opportunity_fails_on_ddb_put_fail(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].fail_put = True

    from backend.crm.service import create_opportunity
    from backend.crm.models import CreateOpportunityRequest

    result = create_opportunity(CreateOpportunityRequest(prospect_id="pr_x"))
    assert result.status == "failed"
    assert result.opportunity_id == ""
    assert "ddb_put_failed" in (result.error or "")


def _seed_opportunity(tables, op_id="op_test", stage="new", **kwargs):
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


def test_advance_opportunity_legal_transition_new_to_qualified(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_a", stage="new")

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_a",
            target_stage="qualified",
        )
    )
    assert result.status == "advanced"
    assert result.prior_stage == "new"
    assert result.new_stage == "qualified"

    op = get_opportunity("op_a")
    assert op is not None
    assert op.stage == "qualified"
    assert op.close_probability == 0.25
    # Non-terminal -> actual_close stays empty
    assert op.actual_close == ""


def test_advance_opportunity_terminal_closed_won_sets_actual_close_and_won_amount(
    tmp_path,
    monkeypatch,
):
    _audit_to_tmp(monkeypatch, tmp_path)
    _disable_close_cap(monkeypatch)  # 22.5k is a legit large deal here
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_w", stage="negotiation", deal_size_usd=20000.0)

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_w",
            target_stage="closed_won",
            won_amount_usd=22500.0,
        )
    )
    assert result.status == "advanced"
    op = get_opportunity("op_w")
    assert op is not None
    assert op.stage == "closed_won"
    assert op.actual_close != ""
    assert op.won_amount_usd == 22500.0
    assert op.close_probability == 1.0


def test_advance_opportunity_closed_won_defaults_won_amount_to_deal_size(
    tmp_path,
    monkeypatch,
):
    _audit_to_tmp(monkeypatch, tmp_path)
    _disable_close_cap(monkeypatch)  # 12k default-fallback is a legit deal here
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_w2", stage="proposal", deal_size_usd=12000.0)

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_w2",
            target_stage="closed_won",
        )
    )
    op = get_opportunity("op_w2")
    assert op is not None
    assert op.won_amount_usd == 12000.0  # fell back to deal_size_usd


def test_advance_opportunity_into_closed_won_retainer(tmp_path, monkeypatch):
    """CRM Phase-2 FSM completion: a target_stage=closed_won_retainer advance
    used to be rejected as unknown_target by the pipeline FSM even though the
    model + request schema declared it. It must now be a legal won-terminal
    close that sets actual_close + close_probability=1.0."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _disable_close_cap(monkeypatch)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables, op_id="op_r", stage="negotiation", deal_size_usd=6000.0, won_amount_usd=6000.0
    )

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_r",
            target_stage="closed_won_retainer",
        )
    )
    assert result.status == "advanced"
    assert result.new_stage == "closed_won_retainer"

    op = get_opportunity("op_r")
    assert op is not None
    assert op.stage == "closed_won_retainer"
    assert op.actual_close != ""
    assert op.close_probability == 1.0


def test_advance_skip_into_retainer_from_new_is_illegal(tmp_path, monkeypatch):
    """closed_won_retainer has the same geometry as closed_won — no skipping
    straight from `new`."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_r2", stage="new")

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_r2",
            target_stage="closed_won_retainer",
        )
    )
    assert result.status == "invalid_transition"
    assert result.error and "illegal_transition" in result.error


def test_advance_does_not_project_to_hivemind_when_flag_off(tmp_path, monkeypatch):
    """The Hivemind projection hook honours the kill-switch: with
    SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED=false a normal advance must not
    attempt any graph write (projection is default-ON, so this asserts the
    explicit-disable path)."""
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", "false")
    from backend.common.settings import reload_settings

    reload_settings()
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_np", stage="new")

    # Sentinel: if the projection ran it would call get_client(); blow up if so.
    import backend.crm.hivemind_projection as hpmod

    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("get_client must not be called when flag is off")

    monkeypatch.setattr(hpmod, "get_client", _boom)

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_np",
            target_stage="qualified",
        )
    )
    assert result.status == "advanced"
    assert called["n"] == 0
    reload_settings()


def test_retainer_close_is_graded_as_win_to_strategy(monkeypatch):
    """Regression: closed_won_retainer must be dispatched to strategy as a WIN
    (graded 1.0). Before the FSM completion it could never reach this branch;
    the legacy else-branch would have mis-graded it as a loss."""
    import backend.crm.service as svc
    from backend.crm.models import Opportunity

    captured: dict[str, Any] = {}

    class _Settings:
        gateway_urls = {"strategy": "https://strategy.local"}
        shared_hmac_key = "k"

    class _Resp:
        status_code = 200

    def _fake_post(base, path, payload, retries=2):
        captured["payload"] = payload
        return _Resp()

    monkeypatch.setattr(svc, "get_settings", lambda: _Settings())
    monkeypatch.setattr(svc, "signed_post_json_sync", _fake_post)

    svc._dispatch_outcome_to_strategy(
        Opportunity(opportunity_id="op_z", prospect_id="pr_z", stage="closed_won_retainer")
    )
    assert captured["payload"]["won"] is True
    assert captured["payload"]["outcome"] == 1.0


# --- FIN-10: hard closed-won amount cap (fail-closed) -----------------------


def test_advance_closed_won_over_cap_is_blocked(tmp_path, monkeypatch):
    """A close above SAMUS_CRM_MAX_CLOSE_AMOUNT_USD (default $1000) must NOT
    advance; the opportunity stays in its current stage + a loud anomaly event
    is emitted; the caller gets a structured blocked_over_cap result."""
    audit_path = tmp_path / "crm_audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    # Cap active out of the box at the default $1000 (no env override).
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_cap", stage="negotiation", deal_size_usd=500.0)

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_cap",
            target_stage="closed_won",
            won_amount_usd=50000.0,  # crafted-large amount, over the $1000 cap
            notes="payment_ref=evt_attacker",
        )
    )
    # Fail-closed: structured blocked result, NOT an exception.
    assert result.status == "blocked_over_cap"
    assert result.new_stage == "negotiation"  # stage unchanged
    assert "closed_won_blocked_over_cap" in (result.error or "")
    # The opportunity was NOT advanced.
    op = get_opportunity("op_cap")
    assert op is not None
    assert op.stage == "negotiation"
    assert op.actual_close in ("", None)
    # Loud anomaly evidence event landed in the ledger.
    assert audit_path.exists()
    import json as _json

    lines = [
        _json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    rejected = [r for r in lines if r.get("status") == "rejected"]
    assert rejected
    assert rejected[-1]["action"] == "advance_opportunity"


def test_advance_closed_won_at_cap_advances(tmp_path, monkeypatch):
    """A close exactly at the cap is legitimate and advances normally."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_atcap", stage="negotiation", deal_size_usd=500.0)

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_atcap",
            target_stage="closed_won",
            won_amount_usd=1000.0,  # exactly at the default cap
        )
    )
    assert result.status == "advanced"
    op = get_opportunity("op_atcap")
    assert op is not None
    assert op.stage == "closed_won"
    assert op.won_amount_usd == 1000.0


def test_advance_closed_won_cap_disabled_allows_large_close(tmp_path, monkeypatch):
    """cap <= 0 disables the check — a large close advances."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _disable_close_cap(monkeypatch)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_nocap", stage="negotiation", deal_size_usd=500.0)

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_nocap",
            target_stage="closed_won",
            won_amount_usd=999999.0,
        )
    )
    assert result.status == "advanced"
    op = get_opportunity("op_nocap")
    assert op is not None
    assert op.stage == "closed_won"
    assert op.won_amount_usd == 999999.0


def test_close_opportunity_from_payment_over_cap_is_blocked(tmp_path, monkeypatch):
    """A forged/inflated Stripe payment-close is blocked at the SQS/webhook
    boundary; the worker sees a structured blocked_over_cap status, not a raise."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_pay_cap")] = {
        "opportunity_id": "op_pay_cap",
        "prospect_id": "pr_pay",
        "stage": "negotiation",
        "deal_size_usd": 500.0,
        "close_probability": 0.6,
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-10T00:00:00Z",
    }
    from backend.crm.service import close_opportunity_from_payment, get_opportunity

    result = close_opportunity_from_payment(
        opportunity_id="op_pay_cap",
        won_amount_usd=100000.0,  # crafted-large Stripe amount_total
        payment_ref="evt_forged",
    )
    assert result.status == "blocked_over_cap"
    op = get_opportunity("op_pay_cap")
    assert op is not None
    assert op.stage == "negotiation"  # NOT advanced to closed_won


def test_advance_opportunity_terminal_closed_lost_sets_actual_close_and_reason(
    tmp_path,
    monkeypatch,
):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_l", stage="proposal")

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_l",
            target_stage="closed_lost",
            lost_reason="budget_too_tight",
        )
    )
    assert result.status == "advanced"
    op = get_opportunity("op_l")
    assert op is not None
    assert op.stage == "closed_lost"
    assert op.actual_close != ""
    assert op.lost_reason == "budget_too_tight"
    assert op.close_probability == 0.0


def test_advance_opportunity_illegal_transition_returns_invalid(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_x", stage="new")

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    # new -> closed_won is not in the allowed set
    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_x",
            target_stage="closed_won",
        )
    )
    assert result.status == "invalid_transition"
    assert result.prior_stage == "new"
    assert result.new_stage == "new"  # unchanged
    assert "illegal_transition" in (result.error or "")
    # Stage unchanged in DDB
    op = get_opportunity("op_x")
    assert op is not None
    assert op.stage == "new"


def test_advance_opportunity_from_terminal_rejected(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_dead", stage="closed_won")

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_dead",
            target_stage="qualified",
        )
    )
    assert result.status == "invalid_transition"
    assert "terminal_stage" in (result.error or "")


def test_advance_opportunity_not_found_returns_not_found(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_nope",
            target_stage="qualified",
        )
    )
    assert result.status == "not_found"
    assert result.error == "opportunity_not_found"


def test_advance_opportunity_ddb_put_failure_returns_failed(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_p", stage="new")
    tables["_opportunities_table"].fail_put = True

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p",
            target_stage="qualified",
        )
    )
    assert result.status == "failed"
    assert "ddb_put_failed" in (result.error or "")


def test_advance_opportunity_audit_records_invalid_transition(tmp_path, monkeypatch):
    audit_path = tmp_path / "crm_audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, op_id="op_audit", stage="new")

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_audit",
            target_stage="closed_won",
        )
    )
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    rejected = [r for r in lines if r.get("status") == "rejected"]
    assert rejected
    assert rejected[0]["action"] == "advance_opportunity"


def test_audit_ledger_captures_conversion(tmp_path, monkeypatch):
    audit_path = tmp_path / "crm_audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    tables = _patch_tables(monkeypatch)
    _seed_lead(tables)
    from backend.crm.service import convert_lead_to_prospect
    from backend.crm.models import ConvertLeadRequest

    convert_lead_to_prospect(ConvertLeadRequest(lead_id="lead_x", assigned_to="ops@hf.tech"))
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    assert any(rec.get("service") == "crm" and rec.get("action") == "convert_lead" for rec in lines)
    # PII redaction: full email NOT in audit body (email_tail only)
    text = audit_path.read_text(encoding="utf-8")
    assert "jane@acme.com" not in text
    assert "Manual outreach is killing us" not in text


# ---------------------------------------------------------------------------
# Phase 4: operator-task create / update / lifecycle auto-generators
# ---------------------------------------------------------------------------


def test_create_operator_task_happy_path(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    from backend.crm.service import create_operator_task
    from backend.crm.models import CreateOperatorTaskRequest

    req = CreateOperatorTaskRequest(
        kind="review",
        title="Review new lead",
        description="Lead from intake form",
        assignee="ops@hf.tech",
        related_entity_kind="lead",
        related_entity_id="lead_abc",
        source="intake_auto",
        source_ref="lead_abc",
    )
    result = create_operator_task(req)
    assert result.status == "created"
    assert result.operator_task_id.startswith("ot_")
    assert result.error is None
    assert len(tables["_operator_tasks_table"].items) == 1
    row = next(iter(tables["_operator_tasks_table"].items.values()))
    assert row["kind"] == "review"
    assert row["status"] == "open"
    assert row["created_at"]


def test_create_operator_task_fails_on_ddb_put(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].fail_put = True
    from backend.crm.service import create_operator_task
    from backend.crm.models import CreateOperatorTaskRequest

    result = create_operator_task(
        CreateOperatorTaskRequest(
            kind="follow_up",
            title="t",
        )
    )
    assert result.status == "failed"
    assert "ddb_put_failed" in (result.error or "")
    assert result.operator_task_id == ""


def test_update_operator_task_open_to_in_progress(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1",
        "kind": "review",
        "title": "x",
        "status": "open",
        "description": "",
    }
    from backend.crm.service import update_operator_task
    from backend.crm.models import UpdateOperatorTaskRequest

    result = update_operator_task(
        UpdateOperatorTaskRequest(
            operator_task_id="ot_1",
            status="in_progress",
        )
    )
    assert result.status == "updated"
    assert result.prior_status == "open"
    assert result.new_status == "in_progress"
    assert (
        tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")]["status"]
        == "in_progress"
    )


def test_update_operator_task_in_progress_to_done_sets_completed_at(
    tmp_path,
    monkeypatch,
):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1",
        "kind": "call",
        "title": "x",
        "status": "in_progress",
        "description": "",
    }
    from backend.crm.service import update_operator_task
    from backend.crm.models import UpdateOperatorTaskRequest

    result = update_operator_task(
        UpdateOperatorTaskRequest(
            operator_task_id="ot_1",
            status="done",
            notes="Spoke with owner.",
        )
    )
    assert result.status == "updated"
    row = tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")]
    assert row["status"] == "done"
    assert row["completed_at"]  # auto-filled
    assert "Spoke with owner." in row["description"]


def test_update_operator_task_done_to_open_rejected(tmp_path, monkeypatch):
    """Backwards transition (terminal -> open) should reject."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1",
        "kind": "review",
        "title": "x",
        "status": "done",
        "description": "",
    }
    from backend.crm.service import update_operator_task
    from backend.crm.models import UpdateOperatorTaskRequest

    result = update_operator_task(
        UpdateOperatorTaskRequest(
            operator_task_id="ot_1",
            status="open",
        )
    )
    assert result.status == "invalid_transition"
    assert result.prior_status == "done"
    assert result.new_status == "done"  # unchanged
    assert "terminal_status_done" in (result.error or "")


def test_update_operator_task_open_to_open_rejected_as_noop(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1",
        "kind": "review",
        "title": "x",
        "status": "open",
    }
    from backend.crm.service import update_operator_task
    from backend.crm.models import UpdateOperatorTaskRequest

    result = update_operator_task(
        UpdateOperatorTaskRequest(
            operator_task_id="ot_1",
            status="open",
        )
    )
    assert result.status == "invalid_transition"
    assert "noop_same_status" in (result.error or "")


def test_update_operator_task_not_found(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.service import update_operator_task
    from backend.crm.models import UpdateOperatorTaskRequest

    result = update_operator_task(
        UpdateOperatorTaskRequest(
            operator_task_id="ot_missing",
            status="in_progress",
        )
    )
    assert result.status == "not_found"
    assert result.error == "operator_task_not_found"


def test_update_operator_task_ddb_put_failure(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_operator_tasks_table"].items[("operator_task_id", "ot_1")] = {
        "operator_task_id": "ot_1",
        "kind": "review",
        "title": "x",
        "status": "open",
    }
    tables["_operator_tasks_table"].fail_put = True
    from backend.crm.service import update_operator_task
    from backend.crm.models import UpdateOperatorTaskRequest

    result = update_operator_task(
        UpdateOperatorTaskRequest(
            operator_task_id="ot_1",
            status="in_progress",
        )
    )
    assert result.status == "failed"
    assert "ddb_put_failed" in (result.error or "")


def test_generate_task_from_lead_shape():
    from backend.crm.service import generate_task_from_lead

    task = generate_task_from_lead(
        lead_id="lead_abc",
        name="Jane Smith",
        email="jane@acme.com",
        company="Acme Roofing",
    )
    assert task["kind"] == "review"
    assert "Acme Roofing" in task["title"]
    assert "Jane Smith" in task["description"]
    assert "jane@acme.com" in task["description"]
    assert task["related_entity_kind"] == "lead"
    assert task["related_entity_id"] == "lead_abc"
    assert task["source"] == "intake_auto"
    assert task["source_ref"] == "lead_abc"
    assert task["due_at"]  # 24h from now
    # Round-trips through the request model.
    from backend.crm.models import CreateOperatorTaskRequest

    req = CreateOperatorTaskRequest(**task)
    assert req.kind == "review"


def test_generate_task_for_stalled_opportunity_shape():
    from backend.crm.service import generate_task_for_stalled_opportunity

    task = generate_task_for_stalled_opportunity(
        opportunity_id="op_xyz",
        prospect_id="pr_acme",
        days_in_stage=7,
    )
    assert task["kind"] == "follow_up"
    assert "op_xyz" in task["title"]
    assert "pr_acme" in task["description"]
    assert "7 days" in task["description"]
    assert task["related_entity_kind"] == "opportunity"
    assert task["related_entity_id"] == "op_xyz"
    assert task["source"] == "opportunity_stalled"
    assert task["source_ref"] == "op_xyz"
    assert task["due_at"]
    from backend.crm.models import CreateOperatorTaskRequest

    req = CreateOperatorTaskRequest(**task)
    assert req.kind == "follow_up"


# ---------------------------------------------------------------------------
# Phase 2 — upsert_conversation + upsert_call_state (voice end-of-call writes)
# ---------------------------------------------------------------------------


def test_upsert_conversation_persists_row(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    from backend.crm.service import upsert_conversation
    from backend.crm.models import Conversation

    conv = Conversation(
        conversation_id="cv_vapi_call_abc",
        prospect_id="pr_acme",
        channel="call",
        status="completed",
        transcript="Hi, this is Morgan...",
        summary="Booked a callback",
        source="vapi",
        source_ref="call_abc",
    )
    ok = upsert_conversation(conv)
    assert ok is True
    stored = tables["_conversations_table"].items[("conversation_id", "cv_vapi_call_abc")]
    assert stored["prospect_id"] == "pr_acme"
    assert stored["source"] == "vapi"
    assert stored["source_ref"] == "call_abc"


def test_upsert_conversation_overwrites_same_id(tmp_path, monkeypatch):
    """Idempotent — second write with the same conversation_id overwrites."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    from backend.crm.service import upsert_conversation
    from backend.crm.models import Conversation

    first = Conversation(conversation_id="cv_x", summary="v1")
    second = Conversation(conversation_id="cv_x", summary="v2")
    assert upsert_conversation(first) is True
    assert upsert_conversation(second) is True
    stored = tables["_conversations_table"].items[("conversation_id", "cv_x")]
    assert stored["summary"] == "v2"
    assert len(tables["_conversations_table"].items) == 1


def test_upsert_conversation_returns_false_on_ddb_failure(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_conversations_table"].fail_put = True
    from backend.crm.service import upsert_conversation
    from backend.crm.models import Conversation

    ok = upsert_conversation(Conversation(conversation_id="cv_x"))
    assert ok is False
    assert len(tables["_conversations_table"].items) == 0


def test_conversation_model_rejects_invalid_channel(tmp_path, monkeypatch):
    """Pydantic Literal enforcement on Conversation.channel surfaces as a
    ValidationError on construction (callers can't sneak garbage past the
    typed contract). Equivalent to the route layer's 422 rejection."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.models import Conversation
    from pydantic import ValidationError
    import pytest as _pytest

    with _pytest.raises(ValidationError):
        Conversation(conversation_id="cv_x", channel="carrier_pigeon")


def test_upsert_conversation_writes_audit_entry(tmp_path, monkeypatch):
    audit_path = tmp_path / "crm_audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    _patch_tables(monkeypatch)
    from backend.crm.service import upsert_conversation
    from backend.crm.models import Conversation

    upsert_conversation(
        Conversation(
            conversation_id="cv_audited",
            source="vapi",
            source_ref="call_z",
        )
    )
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    assert any(rec.get("action") == "upsert_conversation" for rec in lines)


def test_upsert_call_state_persists_row(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    from backend.crm.service import upsert_call_state
    from backend.crm.models import CallState

    state = CallState(
        prospect_id="pr_acme",
        state="completed",
        last_call_id="call_xyz",
        last_outcome="book_call",
    )
    ok = upsert_call_state(state)
    assert ok is True
    stored = tables["_call_state_table"].items[("prospect_id", "pr_acme")]
    assert stored["state"] == "completed"
    assert stored["last_call_id"] == "call_xyz"
    assert stored["updated_at"]  # auto-stamped when caller didn't supply


def test_upsert_call_state_overwrites_same_prospect(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    from backend.crm.service import upsert_call_state
    from backend.crm.models import CallState

    upsert_call_state(CallState(prospect_id="pr_x", state="queued"))
    upsert_call_state(CallState(prospect_id="pr_x", state="completed"))
    stored = tables["_call_state_table"].items[("prospect_id", "pr_x")]
    assert stored["state"] == "completed"
    assert len(tables["_call_state_table"].items) == 1


def test_upsert_call_state_returns_false_on_ddb_failure(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_call_state_table"].fail_put = True
    from backend.crm.service import upsert_call_state
    from backend.crm.models import CallState

    ok = upsert_call_state(CallState(prospect_id="pr_x", state="completed"))
    assert ok is False
    assert len(tables["_call_state_table"].items) == 0


def test_call_state_model_rejects_invalid_state(tmp_path, monkeypatch):
    """``state`` is a Literal — Pydantic blocks unknown FSM values at the
    type boundary, mirroring the route's 422 path."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.models import CallState
    from pydantic import ValidationError
    import pytest as _pytest

    with _pytest.raises(ValidationError):
        CallState(prospect_id="pr_x", state="loitering")


def test_upsert_call_state_writes_audit_entry(tmp_path, monkeypatch):
    audit_path = tmp_path / "crm_audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    _patch_tables(monkeypatch)
    from backend.crm.service import upsert_call_state
    from backend.crm.models import CallState

    upsert_call_state(
        CallState(
            prospect_id="pr_audited",
            state="completed",
            last_call_id="call_audit",
        )
    )
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    assert any(rec.get("action") == "upsert_call_state" for rec in lines)


# ---------------------------------------------------------------------------
# Phase 5 — artifact lifecycle + close-the-loop helpers
# ---------------------------------------------------------------------------


def test_create_artifact_happy_path(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    from backend.crm.service import create_artifact, get_artifact
    from backend.crm.models import CreateArtifactRequest

    req = CreateArtifactRequest(
        kind="seo_audit",
        owner_entity_kind="prospect",
        owner_entity_id="pr_acme",
        title="SEO audit for acme.com",
        inline_data={"seo_score": 72, "issue_count": 4},
        source="seo",
        created_by="samus-seo",
    )
    result = create_artifact(req)
    assert result.status == "created"
    assert result.artifact_id.startswith("ar_")
    assert result.error is None
    assert len(tables["_artifacts_table"].items) == 1

    fetched = get_artifact(result.artifact_id)
    assert fetched is not None
    assert fetched.kind == "seo_audit"
    assert fetched.owner_entity_id == "pr_acme"
    assert fetched.inline_data["seo_score"] == 72


def test_create_artifact_fails_on_ddb_put_fail(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_artifacts_table"].fail_put = True
    from backend.crm.service import create_artifact
    from backend.crm.models import CreateArtifactRequest

    result = create_artifact(
        CreateArtifactRequest(
            kind="proposal",
            owner_entity_kind="opportunity",
            owner_entity_id="op_x",
        )
    )
    assert result.status == "failed"
    assert result.artifact_id == ""
    assert "ddb_put_failed" in (result.error or "")


def test_find_opportunity_for_email_happy(tmp_path, monkeypatch):
    """One contact -> one open opportunity -> returns its id."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_jane")] = {
        "contact_id": "co_jane",
        "prospect_id": "pr_acme",
        "email": "jane@acme.com",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_open")] = {
        "opportunity_id": "op_open",
        "prospect_id": "pr_acme",
        "stage": "qualified",
        "deal_size_usd": 1500.0,
        "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
    }
    from backend.crm.service import find_opportunity_for_email

    assert find_opportunity_for_email("jane@acme.com") == "op_open"


def test_find_opportunity_for_email_no_contact(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.service import find_opportunity_for_email

    assert find_opportunity_for_email("nobody@unknown.com") is None


def test_find_opportunity_for_email_no_open_opp(tmp_path, monkeypatch):
    """Contact exists, but only terminal-stage opportunities -> None."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_dead")] = {
        "contact_id": "co_dead",
        "prospect_id": "pr_dead",
        "email": "dead@acme.com",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_won")] = {
        "opportunity_id": "op_won",
        "prospect_id": "pr_dead",
        "stage": "closed_won",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_lost")] = {
        "opportunity_id": "op_lost",
        "prospect_id": "pr_dead",
        "stage": "closed_lost",
    }
    from backend.crm.service import find_opportunity_for_email

    assert find_opportunity_for_email("dead@acme.com") is None


def test_find_opportunity_for_email_empty_string(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)
    from backend.crm.service import find_opportunity_for_email

    assert find_opportunity_for_email("") is None
    assert find_opportunity_for_email("   ") is None


def test_find_opportunity_for_email_picks_most_recent_open(tmp_path, monkeypatch):
    """When multiple open opps exist, the most-recent one wins."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_contacts_table"].items[("contact_id", "co_x")] = {
        "contact_id": "co_x",
        "prospect_id": "pr_x",
        "email": "x@x.com",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_old")] = {
        "opportunity_id": "op_old",
        "prospect_id": "pr_x",
        "stage": "qualified",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    tables["_opportunities_table"].items[("opportunity_id", "op_new")] = {
        "opportunity_id": "op_new",
        "prospect_id": "pr_x",
        "stage": "proposal",
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-10T00:00:00Z",
    }
    from backend.crm.service import find_opportunity_for_email

    assert find_opportunity_for_email("x@x.com") == "op_new"


def test_close_opportunity_from_payment_advances_to_won(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_pay")] = {
        "opportunity_id": "op_pay",
        "prospect_id": "pr_pay",
        "stage": "negotiation",
        "deal_size_usd": 5000.0,
        "close_probability": 0.6,
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-10T00:00:00Z",
    }
    from backend.crm.service import close_opportunity_from_payment, get_opportunity

    result = close_opportunity_from_payment(
        opportunity_id="op_pay",
        won_amount_usd=999.0,  # under the FIN-10 cap; this test is about the flow
        payment_ref="evt_stripe_xyz",
    )
    assert result.status == "advanced"
    assert result.new_stage == "closed_won"
    op = get_opportunity("op_pay")
    assert op is not None
    assert op.stage == "closed_won"
    assert op.won_amount_usd == 999.0
    assert op.actual_close != ""


def test_close_opportunity_from_payment_propagates_invalid_transition(
    tmp_path,
    monkeypatch,
):
    """If the opp is already closed the wrapper surfaces invalid_transition."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_dead")] = {
        "opportunity_id": "op_dead",
        "stage": "closed_won",
    }
    from backend.crm.service import close_opportunity_from_payment

    result = close_opportunity_from_payment(
        opportunity_id="op_dead",
        won_amount_usd=100.0,
        payment_ref="evt_x",
    )
    assert result.status == "invalid_transition"


def test_close_opportunity_from_payment_payment_ref_in_audit(tmp_path, monkeypatch):
    """payment_ref must land in the audit ledger (notes field)."""
    audit_path = tmp_path / "crm_audit.jsonl"
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(audit_path))
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_aud")] = {
        "opportunity_id": "op_aud",
        "stage": "proposal",
        "deal_size_usd": 750.0,
    }
    from backend.crm.service import close_opportunity_from_payment

    close_opportunity_from_payment(
        opportunity_id="op_aud",
        won_amount_usd=750.0,
        payment_ref="evt_audit_ref_42",
    )
    assert audit_path.exists()
    text = audit_path.read_text(encoding="utf-8")
    assert "evt_audit_ref_42" not in text  # input_payload is hashed
    # But the action + status should be captured.
    lines = [json.loads(l) for l in text.splitlines() if l.strip()]
    advanced = [
        r
        for r in lines
        if r.get("action") == "advance_opportunity" and r.get("status") == "completed"
    ]
    assert advanced
