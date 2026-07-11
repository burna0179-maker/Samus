"""Strategy-integration build, Unit 3 — the attribute + learn loop.

Closes the strategy decide->learn loop. Two halves:

  Part A (attribute) — the Opportunity carries the bandit arm + a reward-signal
    snapshot, and the operator call-logging paths populate them.
  Part B (learn)     — a terminal Opportunity transition best-effort dispatches
    the outcome to the strategy workcell, whose record_outcome builds a
    RewardSignal and updates the hierarchical bandit.

signed_post_json_sync and update_policy_bandit are monkeypatched so every test
stays offline + deterministic. The bandit store is isolated per-test by
conftest.py (JSON tmpfile, truncated each test).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Shared CRM fakes — an in-memory DDB shim mirroring test_crm_service.py
# ---------------------------------------------------------------------------


class _FakeTable:
    """Minimal in-memory DDB shim — get/put + naive single-attr scan."""

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
        if not Key:
            return {}
        key_attr, key_val = next(iter(Key.items()))
        item = self.items.get((key_attr, key_val))
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        out = list(self.items.values())
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


def _stub_strategy_settings(monkeypatch, *, strategy_url="http://strategy.local", hmac="secret"):
    """Make backend.crm.service.get_settings() return a stub with strategy wired."""

    class _S:
        gateway_urls = {"strategy": strategy_url}

    s = _S()
    s.shared_hmac_key = hmac
    # advance_opportunity checks crm_max_close_amount_usd before honoring a
    # closed_won transition; tests use $20k won_amount_usd, so the cap must
    # be permissive. 0.0 means "no cap" downstream.
    s.crm_max_close_amount_usd = 0.0
    import backend.crm.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: s)


class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.text = ""


def _capture_strategy_posts(monkeypatch, *, raises=None, status_code=200):
    """Capture signed_post_json_sync calls on the CRM service; return capture list."""
    posts: list[tuple] = []
    import backend.crm.service as svc

    def _fake_post(base_url, path, payload, **kw):
        posts.append((base_url, path, payload))
        if raises is not None:
            raise raises
        return _Resp(status_code)

    monkeypatch.setattr(svc, "signed_post_json_sync", _fake_post)
    return posts


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


# ===========================================================================
# Part A — attribute: the Opportunity carries the bandit arm + snapshot
# ===========================================================================


def test_opportunity_model_carries_arm_and_reward_snapshot():
    """Opportunity gains industry/policy_family + the four reward-signal fields."""
    from backend.crm.models import Opportunity

    opp = Opportunity(
        opportunity_id="op_x",
        industry="hvac",
        policy_family="fast_quote_mode",
        seo_score=72,
        owner_email=True,
        social_facebook=True,
        social_instagram=False,
    )
    assert opp.industry == "hvac"
    assert opp.policy_family == "fast_quote_mode"
    assert opp.seo_score == 72
    assert opp.owner_email is True
    assert opp.social_facebook is True
    assert opp.social_instagram is False


def test_opportunity_arm_fields_default_for_pre_feature_rows():
    """A pre-this-feature Opportunity (no arm fields) reads back unchanged."""
    from backend.crm.models import Opportunity

    opp = Opportunity(opportunity_id="op_old")
    assert opp.industry == ""
    assert opp.policy_family == ""
    assert opp.seo_score == 0
    assert opp.owner_email is False
    assert opp.social_facebook is False
    assert opp.social_instagram is False


def test_create_opportunity_request_carries_arm_and_snapshot():
    """CreateOpportunityRequest accepts the arm + reward-signal fields."""
    from backend.crm.models import CreateOpportunityRequest

    req = CreateOpportunityRequest(
        prospect_id="pr_acme",
        industry="dentist",
        policy_family="reputation_repair",
        seo_score=40,
        owner_email=True,
        social_facebook=False,
        social_instagram=True,
    )
    assert req.industry == "dentist"
    assert req.policy_family == "reputation_repair"
    assert req.seo_score == 40
    assert req.owner_email is True
    assert req.social_instagram is True


def test_create_opportunity_persists_arm_and_snapshot(tmp_path, monkeypatch):
    """crm.service.create_opportunity stores the arm + snapshot onto the row."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _patch_tables(monkeypatch)

    from backend.crm.service import create_opportunity, get_opportunity
    from backend.crm.models import CreateOpportunityRequest

    result = create_opportunity(
        CreateOpportunityRequest(
            prospect_id="pr_acme",
            industry="hvac",
            policy_family="fast_quote_mode",
            seo_score=63,
            owner_email=True,
            social_facebook=True,
            social_instagram=False,
        )
    )
    assert result.status == "created"

    opp = get_opportunity(result.opportunity_id)
    assert opp is not None
    assert opp.industry == "hvac"
    assert opp.policy_family == "fast_quote_mode"
    assert opp.seo_score == 63
    assert opp.owner_email is True
    assert opp.social_facebook is True
    assert opp.social_instagram is False


def test_log_call_booked_forwards_arm_into_opportunity_request(tmp_path, monkeypatch):
    """A booked log_call threads the arm + snapshot into CreateOpportunityRequest."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.crm import service as crm_service
    from backend.crm.models import CreateOpportunityResult

    captured: dict = {}

    def _create_opportunity(req):
        captured["opportunity_request"] = req
        return CreateOpportunityResult(
            status="created",
            opportunity_id="op_booked",
            ts="2026-05-20T00:00:00Z",
            error=None,
        )

    monkeypatch.setattr(crm_service, "upsert_conversation", lambda c: True)
    monkeypatch.setattr(crm_service, "upsert_call_state", lambda s: True)
    monkeypatch.setattr(crm_service, "get_call_state", lambda pid: None)
    monkeypatch.setattr(crm_service, "create_opportunity", _create_opportunity)

    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_acme",
        company="Acme HVAC",
        outcome="booked",
        notes="booked an audit",
        industry="hvac",
        policy_family="fast_quote_mode",
        seo_score=58,
        owner_email=True,
        social_facebook=False,
        social_instagram=True,
    )
    assert result["ok"] is True
    assert result["opportunity_id"] == "op_booked"

    req = captured["opportunity_request"]
    assert req.industry == "hvac"
    assert req.policy_family == "fast_quote_mode"
    assert req.seo_score == 58
    assert req.owner_email is True
    assert req.social_facebook is False
    assert req.social_instagram is True


def test_log_call_non_booked_does_not_create_opportunity(tmp_path, monkeypatch):
    """A non-booked outcome opens no Opportunity — the arm fields are inert."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.crm import service as crm_service

    created: list = []

    monkeypatch.setattr(crm_service, "upsert_conversation", lambda c: True)
    monkeypatch.setattr(crm_service, "upsert_call_state", lambda s: True)
    monkeypatch.setattr(crm_service, "get_call_state", lambda pid: None)
    monkeypatch.setattr(crm_service, "create_opportunity", lambda req: created.append(req))

    from backend.crm.log_call import log_call

    result = log_call(
        prospect_id="pr_acme",
        outcome="follow_up",
        industry="hvac",
        policy_family="fast_quote_mode",
    )
    assert result["opportunity_id"] == ""
    assert created == []


# ===========================================================================
# Part B — learn: terminal transition dispatches; strategy updates the bandit
# ===========================================================================


def test_terminal_closed_won_dispatches_outcome_to_strategy(tmp_path, monkeypatch):
    """A closed_won transition fires the best-effort strategy dispatch."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables,
        op_id="op_w",
        stage="negotiation",
        prospect_id="pr_acme",
        industry="hvac",
        policy_family="fast_quote_mode",
        seo_score=70,
        owner_email=True,
        social_facebook=True,
        social_instagram=False,
    )
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_w",
            target_stage="closed_won",
            won_amount_usd=20000.0,
        )
    )
    assert result.status == "advanced"
    assert len(posts) == 1
    base_url, path, payload = posts[0]
    assert path == "/strategy/record-outcome"
    assert payload["prospect_id"] == "pr_acme"
    assert payload["industry"] == "hvac"
    assert payload["policy_family"] == "fast_quote_mode"
    assert payload["outcome"] == 1.0  # closed_won -> full reward
    assert payload["won"] is True
    assert payload["seo_score"] == 70
    assert payload["owner_email"] is True
    assert payload["social_facebook"] is True
    assert payload["social_instagram"] is False


def test_terminal_closed_lost_dispatches_zero_outcome(tmp_path, monkeypatch):
    """A closed_lost transition dispatches a graded outcome of 0.0."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables,
        op_id="op_l",
        stage="proposal",
        prospect_id="pr_acme",
        industry="dentist",
        policy_family="reputation_repair",
    )
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_l",
            target_stage="closed_lost",
            lost_reason="ghosted",
        )
    )
    assert result.status == "advanced"
    assert len(posts) == 1
    _base, path, payload = posts[0]
    assert path == "/strategy/record-outcome"
    assert payload["outcome"] == 0.0  # closed_lost -> zero reward
    assert payload["won"] is False
    assert payload["industry"] == "dentist"


def test_non_terminal_transition_does_not_dispatch(tmp_path, monkeypatch):
    """A non-terminal advance (new -> qualified) fires NO strategy dispatch."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables, op_id="op_q", stage="new", industry="hvac", policy_family="fast_quote_mode"
    )
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_q",
            target_stage="qualified",
        )
    )
    assert result.status == "advanced"
    assert posts == []


def test_dispatch_is_best_effort_strategy_outage_does_not_fail_write(
    tmp_path,
    monkeypatch,
):
    """A strategy outage during dispatch must not undo the opportunity write."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables, op_id="op_w2", stage="negotiation", industry="hvac", policy_family="fast_quote_mode"
    )
    _stub_strategy_settings(monkeypatch)
    _capture_strategy_posts(monkeypatch, raises=RuntimeError("strategy down"))

    from backend.crm.service import advance_opportunity, get_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_w2",
            target_stage="closed_won",
        )
    )
    # The opportunity write still committed despite the dispatch raising.
    assert result.status == "advanced"
    opp = get_opportunity("op_w2")
    assert opp is not None
    assert opp.stage == "closed_won"


def test_dispatch_skipped_when_strategy_url_unset(tmp_path, monkeypatch):
    """No strategy URL configured -> dispatch is a clean no-op."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables, op_id="op_w3", stage="negotiation", industry="hvac", policy_family="fast_quote_mode"
    )
    _stub_strategy_settings(monkeypatch, strategy_url="")
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.service import advance_opportunity
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity(
        AdvanceOpportunityRequest(
            opportunity_id="op_w3",
            target_stage="closed_won",
        )
    )
    assert result.status == "advanced"
    assert posts == []  # short-circuited before any POST


def test_close_from_payment_routes_through_single_dispatch_point(
    tmp_path,
    monkeypatch,
):
    """close_opportunity_from_payment closes via advance_opportunity -> dispatches."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables,
        op_id="op_pay",
        stage="negotiation",
        industry="roofing",
        policy_family="storm_damage_mode",
    )
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.service import close_opportunity_from_payment

    result = close_opportunity_from_payment("op_pay", 9000.0, "evt_stripe_1")
    assert result.status == "advanced"
    assert len(posts) == 1
    assert posts[0][2]["industry"] == "roofing"
    assert posts[0][2]["outcome"] == 1.0


def test_advance_with_lifecycle_routes_through_single_dispatch_point(
    tmp_path,
    monkeypatch,
):
    """advance_opportunity_with_lifecycle closes via advance_opportunity -> dispatches."""
    _audit_to_tmp(monkeypatch, tmp_path)
    tables = _patch_tables(monkeypatch)
    _seed_opportunity(
        tables, op_id="op_lc", stage="proposal", industry="hvac", policy_family="fast_quote_mode"
    )
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.service import advance_opportunity_with_lifecycle
    from backend.crm.models import AdvanceOpportunityRequest

    result = advance_opportunity_with_lifecycle(
        AdvanceOpportunityRequest(
            opportunity_id="op_lc",
            target_stage="closed_won",
        )
    )
    assert result.status == "advanced"
    assert len(posts) == 1
    assert posts[0][1] == "/strategy/record-outcome"


# ---------------------------------------------------------------------------
# Part B — strategy.service.record_outcome builds a RewardSignal + updates bandit
# ---------------------------------------------------------------------------


def _capture_bandit(monkeypatch):
    """Capture portfolio_manager.update_policy_bandit calls on strategy.service."""
    calls: list[dict] = []
    import backend.strategy.service as svc

    def _fake_update(industry, policy_family, outcome, *, reward_signal=None):
        calls.append(
            {
                "industry": industry,
                "policy_family": policy_family,
                "outcome": outcome,
                "reward_signal": reward_signal,
            }
        )

    monkeypatch.setattr(svc.portfolio_manager, "update_policy_bandit", _fake_update)
    return calls


async def _record(req):
    from backend.strategy.service import record_outcome

    return await record_outcome(req)


def test_record_outcome_with_arm_builds_reward_signal_and_updates_bandit(monkeypatch):
    """A request carrying industry+policy_family updates the hierarchical bandit."""
    import asyncio

    calls = _capture_bandit(monkeypatch)

    from backend.strategy.models import RecordOutcomeRequest

    req = RecordOutcomeRequest(
        prospect_id="pr_acme",
        won=True,
        industry="hvac",
        policy_family="fast_quote_mode",
        outcome=1.0,
        seo_score=80,
        owner_email=True,
        social_facebook=True,
        social_instagram=False,
    )
    resp = asyncio.run(_record(req))
    assert resp.recorded is True

    assert len(calls) == 1
    call = calls[0]
    assert call["industry"] == "hvac"
    assert call["policy_family"] == "fast_quote_mode"
    assert call["outcome"] == 1.0

    sig = call["reward_signal"]
    assert sig is not None
    assert sig.outcome == 1.0
    assert sig.owner_email is True
    assert sig.social_facebook is True
    assert sig.social_instagram is False
    # seo_score 0-100 normalised to [0,1]: 80 -> 0.8
    assert abs(sig.seo_score - 0.8) < 1e-9
    # owner_email present -> contactability derives to 1.0
    assert sig.contactability == 1.0
    # no infra telemetry crosses the boundary -> neutral 0.5
    assert sig.infrastructure_health == 0.5


def test_record_outcome_without_owner_email_uses_neutral_contactability(monkeypatch):
    """Absent owner_email -> contactability derives to the neutral 0.5."""
    import asyncio

    calls = _capture_bandit(monkeypatch)

    from backend.strategy.models import RecordOutcomeRequest

    req = RecordOutcomeRequest(
        prospect_id="pr_x",
        won=False,
        industry="dentist",
        policy_family="reputation_repair",
        outcome=0.0,
        seo_score=20,
        owner_email=False,
    )
    asyncio.run(_record(req))
    sig = calls[0]["reward_signal"]
    assert sig.contactability == 0.5
    assert abs(sig.seo_score - 0.2) < 1e-9


def test_record_outcome_without_arm_skips_bandit(monkeypatch):
    """A pre-this-feature request (no industry/policy_family) makes no bandit call."""
    import asyncio

    calls = _capture_bandit(monkeypatch)

    from backend.strategy.models import RecordOutcomeRequest

    req = RecordOutcomeRequest(prospect_id="pr_legacy", won=True)
    resp = asyncio.run(_record(req))
    assert resp.recorded is True
    assert calls == []  # no bandit call on the legacy path


def test_record_outcome_partial_arm_skips_bandit(monkeypatch):
    """industry set but policy_family empty -> still no bandit call (need both)."""
    import asyncio

    calls = _capture_bandit(monkeypatch)

    from backend.strategy.models import RecordOutcomeRequest

    req = RecordOutcomeRequest(
        prospect_id="pr_partial",
        won=True,
        industry="hvac",
        policy_family="",
    )
    asyncio.run(_record(req))
    assert calls == []


def test_record_outcome_legacy_pattern_path_intact(monkeypatch):
    """The heuristic boost/penalize_pattern calls still fire (legacy behaviour kept)."""
    import asyncio

    _capture_bandit(monkeypatch)
    boosted: list[str] = []
    penalized: list[str] = []
    import backend.strategy.service as svc

    monkeypatch.setattr(svc.engine, "boost_pattern", lambda p: boosted.append(p))
    monkeypatch.setattr(svc.engine, "penalize_pattern", lambda p: penalized.append(p))

    from backend.strategy.models import RecordOutcomeRequest

    asyncio.run(_record(RecordOutcomeRequest(prospect_id="pr_a", won=True)))
    asyncio.run(_record(RecordOutcomeRequest(prospect_id="pr_b", won=False)))
    assert boosted == ["similar_prospects"]
    assert penalized == ["strategy_path"]


def test_record_outcome_real_bandit_records_trial(monkeypatch):
    """End-to-end: record_outcome with an arm leaves a real trial on the durable
    bandit (conftest isolates the JSON-file store per test)."""
    import asyncio
    from backend.strategy import portfolio_manager as pm

    pm.reset_bandit()

    from backend.strategy.models import RecordOutcomeRequest

    req = RecordOutcomeRequest(
        prospect_id="pr_acme",
        won=True,
        industry="hvac",
        policy_family="fast_quote_mode",
        outcome=1.0,
        seo_score=50,
        owner_email=True,
    )
    asyncio.run(_record(req))

    stats = pm.get_policy_bandit_stats("hvac")
    arm = stats.get("hvac::fast_quote_mode")
    assert arm is not None
    assert arm["trials"] == 1
    # density-weighted reward (RewardSignal supplied) -> wins > the raw 1.0.
    assert arm["wins"] > 1.0
