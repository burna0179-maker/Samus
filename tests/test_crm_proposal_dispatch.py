"""Inbound deal funnel — Unit P: CRM auto-drafts a proposal on the `proposal` stage.

``advance_opportunity(target_stage="proposal")`` fires ``_dispatch_intake_to_proposal``,
which POSTs a ``generate_proposal`` TaskEnvelope at the gateway's ``/dispatch/proposal``
so a draft proposal is waiting in the artifact list by review time. The dispatch is
best-effort: a missing gateway URL / HMAC key, or any transport failure, must never
undo the stage advance. ``signed_post_json_sync`` is stubbed so every test is offline.
"""

from __future__ import annotations

from typing import Any


class _FakeTable:
    """Minimal in-memory DDB shim — get/put + naive scan."""

    def __init__(self) -> None:
        self.items: dict[tuple, dict[str, Any]] = {}
        self.pk_attr: str | None = None

    def put_item(self, Item):
        key_attr = self.pk_attr or next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key):
        if not Key:
            return {}
        key_attr, key_val = next(iter(Key.items()))
        item = self.items.get((key_attr, key_val))
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        out = list(self.items.values())
        return {"Items": out[: kwargs.get("Limit", 50)]}


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


class _Resp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


def _stub_settings(monkeypatch, *, gateway_url="http://gateway.local", hmac="secret"):
    class _S:
        gateway_urls = {"gateway": gateway_url} if gateway_url else {}

    s = _S()
    s.shared_hmac_key = hmac
    import backend.crm.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: s)


def _capture_posts(monkeypatch):
    posts: list[tuple] = []
    import backend.crm.service as svc

    def _fake_post(base_url, path, payload, **kw):
        posts.append((base_url, path, payload))
        return _Resp(200)

    monkeypatch.setattr(svc, "signed_post_json_sync", _fake_post)
    return posts


def _seed_opportunity(tables, opportunity_id, stage, **extra):
    tables["_opportunities_table"].items[("opportunity_id", opportunity_id)] = {
        "opportunity_id": opportunity_id,
        "prospect_id": extra.get("prospect_id", "pr_acme"),
        "stage": stage,
        "name": extra.get("name", ""),
        "deal_size_usd": extra.get("deal_size_usd", 0.0),
        "next_step": extra.get("next_step", ""),
        "close_probability": 0.1,
        "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
    }


def test_advance_to_proposal_dispatches_generate_proposal(tmp_path, monkeypatch):
    """qualified -> proposal fires a generate_proposal envelope at the gateway."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables,
        "op_p1",
        "qualified",
        name="Acme HVAC retainer",
        next_step="send the automation proposal",
        deal_size_usd=2500.0,
    )
    _stub_settings(monkeypatch)
    posts = _capture_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p1",
            target_stage="proposal",
        )
    )
    assert result.status == "advanced"
    assert len(posts) == 1
    base, path, payload = posts[0]
    assert base == "http://gateway.local"
    assert path == "/dispatch/proposal"
    assert payload["metadata"]["action"] == "generate_proposal"
    assert payload["payload"]["opportunity_id"] == "op_p1"
    assert payload["payload"]["prospect_id"] == "pr_acme"
    intake = payload["payload"]["intake"]
    assert intake["client_name"] == "Acme HVAC retainer"
    assert intake["business_goal"] == "send the automation proposal"
    assert intake["budget_usd"] == 2500.0


def test_dispatched_payload_validates_as_a_proposal_request(tmp_path, monkeypatch):
    """The dispatched payload round-trips through the proposal workcell's own
    ProposalRequest / OnboardingIntake models (both extra='forbid')."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_p2", "qualified")
    _stub_settings(monkeypatch)
    posts = _capture_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity

    advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p2",
            target_stage="proposal",
        )
    )
    assert len(posts) == 1
    inner = posts[0][2]["payload"]

    from backend.proposal.models import ProposalRequest

    req = ProposalRequest.model_validate(inner)
    assert req.opportunity_id == "op_p2"
    # Empty want-lists -> proposal compiles an empty workflow / needs_review.
    assert req.intake.triggers_wanted == []
    assert req.intake.actions_wanted == []


def test_unnamed_opportunity_gets_a_fallback_client_name(tmp_path, monkeypatch):
    """An Opportunity with no `name` / `next_step` still produces a valid
    OnboardingIntake — the fallbacks fill client_name + business_goal."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_p2b", "qualified", name="", next_step="")
    _stub_settings(monkeypatch)
    posts = _capture_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity

    advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p2b",
            target_stage="proposal",
        )
    )
    intake = posts[0][2]["payload"]["intake"]
    assert intake["client_name"]  # fallback "Opportunity <id-tail>"
    assert intake["business_goal"]  # fallback scoping note
    assert intake["budget_usd"] is None  # deal_size_usd 0.0 -> None


def test_non_proposal_advance_dispatches_nothing(tmp_path, monkeypatch):
    """new -> qualified is not the proposal stage — no proposal dispatch."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_p3", "new")
    _stub_settings(monkeypatch)
    posts = _capture_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p3",
            target_stage="qualified",
        )
    )
    assert result.status == "advanced"
    assert posts == []


def test_proposal_dispatch_skipped_when_gateway_unconfigured(tmp_path, monkeypatch):
    """No gateway URL -> the dispatch short-circuits; the advance still succeeds."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_p4", "qualified")
    _stub_settings(monkeypatch, gateway_url="")
    posts = _capture_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p4",
            target_stage="proposal",
        )
    )
    assert result.status == "advanced"
    assert posts == []


def test_proposal_dispatch_failure_does_not_undo_the_advance(tmp_path, monkeypatch):
    """A transport failure inside the dispatch is swallowed — the opportunity
    is still persisted in the `proposal` stage."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(tables, "op_p5", "qualified")
    _stub_settings(monkeypatch)

    import backend.crm.service as svc

    def _boom(*a, **k):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(svc, "signed_post_json_sync", _boom)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity, get_opportunity

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_p5",
            target_stage="proposal",
        )
    )
    assert result.status == "advanced"
    opp = get_opportunity("op_p5")
    assert opp is not None
    assert opp.stage == "proposal"
