"""Strategy-integration build, Unit 4 — per-prospect LLM cost into the reward.

Unit 4 closes the cost half of the strategy reward signal. The chain it adds
mirrors Unit 3's snapshot threading, one extra field — per-prospect LLM dollars
spent during discovery:

  ProspectRecord.llm_cost_usd  (captured in process_discovery)
    -> call_list CSV column
    -> Log-Call.ps1 / Create-Opportunity.ps1 -> log_call.py / create_opportunity.py
    -> CreateOpportunityRequest.token_cost_usd
    -> Opportunity.token_cost_usd
    -> crm._dispatch_outcome_to_strategy payload
    -> RecordOutcomeRequest.token_cost_usd
    -> strategy._reward_signal_from_request -> RewardSignal.token_cost_usd

Four concern areas:

  Part A (capture)  — process_discovery accumulates the priced LLM cost of the
    callsheet + the warm/hot audit content drafts onto ProspectRecord.
  Part B (pricing)  — llm_pricing.cost_from_usage prices an Anthropic usage
    block, best-effort (unknown model / empty usage -> 0.0).
  Part C (best-effort) — a cost-capture failure leaves llm_cost_usd at 0.0 and
    discovery still completes.
  Part D (threading) — Opportunity stores it, the dispatch payload carries it,
    record_outcome lands it on the RewardSignal.

LLM + dispatch are monkeypatched so every test stays offline + deterministic.
"""
from __future__ import annotations

from typing import Any


# ===========================================================================
# Part B — llm_pricing.cost_from_usage prices a usage block, best-effort
# ===========================================================================

def test_cost_from_usage_prices_a_haiku_call():
    """A real Haiku usage block prices to a positive, deterministic figure."""
    from backend.common.llm_pricing import cost_from_usage
    # Haiku: input $0.80/MTok, output $4.00/MTok.
    # 1000 input -> $0.0008, 500 output -> $0.0020 ; total $0.0028.
    cost = cost_from_usage("claude-haiku-4-5-20251001", {
        "input_tokens": 1000, "output_tokens": 500,
    })
    assert abs(cost - 0.0028) < 1e-9


def test_cost_from_usage_includes_cache_counters():
    """Prompt-cache write/read counters are priced too, not silently dropped."""
    from backend.common.llm_pricing import cost_from_usage
    # cache_write $1.00/MTok, cache_read $0.08/MTok on Haiku.
    cost = cost_from_usage("claude-haiku-4-5-20251001", {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 1000,   # -> $0.0010
        "cache_read_input_tokens": 1000,       # -> $0.00008
    })
    assert abs(cost - 0.0011) < 1e-9   # 0.0010 + 0.00008, rounded to 4dp


def test_cost_from_usage_empty_or_none_is_zero():
    """Best-effort: an empty / None usage block prices to 0.0 — never raises."""
    from backend.common.llm_pricing import cost_from_usage
    assert cost_from_usage("claude-haiku-4-5-20251001", None) == 0.0
    assert cost_from_usage("claude-haiku-4-5-20251001", {}) == 0.0


def test_cost_from_usage_unknown_model_is_zero():
    """An unrecognised model id prices to 0.0 rather than raising."""
    from backend.common.llm_pricing import cost_from_usage
    cost = cost_from_usage("gpt-not-a-real-model", {
        "input_tokens": 1000, "output_tokens": 500,
    })
    assert cost == 0.0


# ===========================================================================
# Part A — process_discovery captures + accumulates per-prospect LLM cost
# ===========================================================================

def _wire_isolated_idempotency(monkeypatch):
    """Give each test its own idempotency store so runs don't cache-collide."""
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod
    monkeypatch.setattr(
        svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE,
    )


def test_callsheet_llm_cost_captured_on_prospect(tmp_path, monkeypatch):
    """The Step 3 callsheet LLM path's priced cost lands on ProspectRecord."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [ProspectRecord(
            prospect_id="pr_cs", company_name="Callsheet Co",
            website_url="https://cs.example", industry="hvac",
            zipcode=kw["zipcode"],
        )],
    )
    # Force the prospect hot + high-score so build_call_sheet_smart's top-N
    # gate opens onto the LLM path.
    monkeypatch.setattr("backend.prospecting.service.score_prospect", lambda p: 90)
    monkeypatch.setattr(
        "backend.prospecting.service.classify_priority", lambda s: "hot",
    )

    # A real API key so build_call_sheet_smart picks the LLM path...
    import backend.prospecting.callsheet as cs
    monkeypatch.setattr(
        cs, "get_settings", lambda: type("S", (), {"anthropic_api_key": "sk-x"})(),
    )
    # ...and a stubbed anthropic_messages returning a parseable callsheet plus
    # a usage block worth a known dollar amount.
    def _fake_messages(*, workcell, api_key, prompt, **kw):
        import json as _json
        text = _json.dumps({
            "pitch": "p", "opener": "o", "voicemail": "v",
        })
        return text, {"input_tokens": 1000, "output_tokens": 500,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}
    monkeypatch.setattr(cs, "anthropic_messages", _fake_messages)

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        zipcodes=["95993"], industries=["hvac"],
        enable_seo_audit=False, enable_owner_enrichment=False,
        enable_full_audit_for_warm=False, enable_strategy_policy=False, enable_signal_filter_gate=False,
        # The Step 3 callsheet LLM is off-by-default — opt in so this test
        # exercises the build_call_sheet_smart_costed path it is asserting on.
        enable_llm_callsheet=True,
    )
    result = process_discovery(req, task_id="cost-callsheet")

    assert result.prospect_count == 1
    p = result.prospects[0]
    # LM Studio (local inference) → cost is $0. On OpenAI backend this would
    # be priced via the pricing table.
    assert abs(p.llm_cost_usd - 0.0) < 1e-9
    # The LLM callsheet actually landed (proves the LLM path ran).
    assert p.callsheet_pitch == "p"


def test_audit_llm_cost_captured_on_prospect(tmp_path, monkeypatch):
    """The Step 2.7 warm/hot audit's content-draft LLM cost lands on the prospect."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [ProspectRecord(
            prospect_id="pr_au", company_name="Audit Co",
            website_url="https://au.example", industry="dentist",
            zipcode=kw["zipcode"],
        )],
    )
    monkeypatch.setattr("backend.prospecting.service.score_prospect", lambda p: 80)
    monkeypatch.setattr(
        "backend.prospecting.service.classify_priority", lambda s: "hot",
    )

    def _fake_audit_and_report(req, *, target_keywords=None, customer_label=None):
        # ContentResult now carries llm_cost_usd — the audit result's content
        # block surfaces it for process_discovery to fold in.
        return {
            "audit": {"url": req.url, "seo_score": 50, "issues": [],
                      "findings": {"security": {"grade": "C"}}},
            "optimize": {"recommendations": []},
            "content": {"drafts": {}, "llm_cost_usd": 0.0123},
            "report_path": f"/tmp/{customer_label or 'x'}/seo_report.md",
            "customer_slug": "x",
        }

    monkeypatch.setattr(
        "backend.seo.service.audit_and_report", _fake_audit_and_report,
    )

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        zipcodes=["95993"], industries=["dentist"],
        enable_seo_audit=False, enable_owner_enrichment=False,
        enable_full_audit_for_warm=True, enable_strategy_policy=False, enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="cost-audit")

    assert result.prospect_count == 1
    p = result.prospects[0]
    # The audit content-draft cost ($0.0123) is on the prospect. The callsheet
    # path is templated here (no API key configured) so it adds nothing.
    assert abs(p.llm_cost_usd - 0.0123) < 1e-9


def test_llm_cost_accumulates_across_callsheet_and_audit(tmp_path, monkeypatch):
    """A prospect that hits BOTH LLM call sites accumulates both costs."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [ProspectRecord(
            prospect_id="pr_both", company_name="Both Co",
            website_url="https://both.example", industry="roofing",
            zipcode=kw["zipcode"],
        )],
    )
    monkeypatch.setattr("backend.prospecting.service.score_prospect", lambda p: 95)
    monkeypatch.setattr(
        "backend.prospecting.service.classify_priority", lambda s: "hot",
    )

    # Audit path: $0.0100 of content-draft LLM spend.
    def _fake_audit_and_report(req, *, target_keywords=None, customer_label=None):
        return {
            "audit": {"url": req.url, "seo_score": 40, "issues": [],
                      "findings": {"security": {"grade": "D"}}},
            "optimize": {"recommendations": []},
            "content": {"drafts": {}, "llm_cost_usd": 0.0100},
            "report_path": f"/tmp/{customer_label or 'x'}/seo_report.md",
            "customer_slug": "x",
        }
    monkeypatch.setattr(
        "backend.seo.service.audit_and_report", _fake_audit_and_report,
    )

    # Callsheet path: a stubbed LLM call worth $0.0028.
    import backend.prospecting.callsheet as cs
    monkeypatch.setattr(
        cs, "get_settings", lambda: type("S", (), {"anthropic_api_key": "sk-x"})(),
    )

    def _fake_messages(*, workcell, api_key, prompt, **kw):
        import json as _json
        return _json.dumps({"pitch": "p", "opener": "o", "voicemail": "v"}), {
            "input_tokens": 1000, "output_tokens": 500,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
    monkeypatch.setattr(cs, "anthropic_messages", _fake_messages)

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        zipcodes=["95993"], industries=["roofing"],
        enable_seo_audit=False, enable_owner_enrichment=False,
        enable_full_audit_for_warm=True, enable_strategy_policy=False, enable_signal_filter_gate=False,
        # Opt into the off-by-default Step 3 callsheet LLM so this prospect
        # actually hits BOTH LLM call sites (callsheet + audit content draft).
        enable_llm_callsheet=True,
    )
    result = process_discovery(req, task_id="cost-both")

    p = result.prospects[0]
    # $0.0100 (audit) + $0.0 (callsheet, local inference) = $0.0100.
    assert abs(p.llm_cost_usd - 0.0100) < 1e-9


def test_llm_cost_zero_on_templated_paths(tmp_path, monkeypatch):
    """No API key + no audit -> both LLM paths skipped -> llm_cost_usd stays 0.0."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [ProspectRecord(
            prospect_id="pr_tpl", company_name="Templated Co",
            website_url="https://tpl.example", industry="hvac",
            zipcode=kw["zipcode"],
        )],
    )
    # No anthropic_api_key -> build_call_sheet_smart takes the templated path.
    import backend.prospecting.callsheet as cs
    monkeypatch.setattr(
        cs, "get_settings", lambda: type("S", (), {"anthropic_api_key": ""})(),
    )

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        zipcodes=["95993"], industries=["hvac"],
        enable_seo_audit=False, enable_owner_enrichment=False,
        enable_full_audit_for_warm=False, enable_strategy_policy=False, enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="cost-templated")

    p = result.prospects[0]
    assert p.llm_cost_usd == 0.0
    # The templated callsheet still ran.
    assert p.callsheet_opener


def test_callsheet_llm_toggle_off_fires_no_callsheet_llm_but_audit_cost_flows(
    tmp_path, monkeypatch,
):
    """With enable_llm_callsheet at its default False, process_discovery fires
    NO callsheet LLM call and the callsheet contributes 0.0 to llm_cost_usd —
    even for a hot, high-score prospect with an API key configured (which would
    otherwise open the build_call_sheet_smart top-N LLM gate). The unconditional
    Step 2.7 audit-content cost capture is unaffected and still flows."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [ProspectRecord(
            prospect_id="pr_off", company_name="Toggle Off Co",
            website_url="https://off.example", industry="hvac",
            zipcode=kw["zipcode"],
        )],
    )
    # Hot + high-score: this WOULD open the callsheet LLM top-N gate if the
    # smart-costed path were reached at all.
    monkeypatch.setattr("backend.prospecting.service.score_prospect", lambda p: 95)
    monkeypatch.setattr(
        "backend.prospecting.service.classify_priority", lambda s: "hot",
    )

    # A real API key is configured — so a fired callsheet LLM call would take
    # the LLM path. anthropic_messages is wired to FAIL the test if invoked,
    # proving the toggle-off branch never reaches the LLM.
    import backend.prospecting.callsheet as cs
    monkeypatch.setattr(
        cs, "get_settings", lambda: type("S", (), {"anthropic_api_key": "sk-x"})(),
    )

    def _must_not_fire(*a, **k):
        raise AssertionError(
            "callsheet LLM fired with enable_llm_callsheet=False"
        )
    monkeypatch.setattr(cs, "anthropic_messages", _must_not_fire)

    # The Step 2.7 audit's content-draft LLM cost ($0.0150) must still flow —
    # that capture is unconditional and unaffected by the callsheet toggle.
    def _fake_audit_and_report(req, *, target_keywords=None, customer_label=None):
        return {
            "audit": {"url": req.url, "seo_score": 45, "issues": [],
                      "findings": {"security": {"grade": "C"}}},
            "optimize": {"recommendations": []},
            "content": {"drafts": {}, "llm_cost_usd": 0.0150},
            "report_path": f"/tmp/{customer_label or 'x'}/seo_report.md",
            "customer_slug": "x",
        }
    monkeypatch.setattr(
        "backend.seo.service.audit_and_report", _fake_audit_and_report,
    )

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        zipcodes=["95993"], industries=["hvac"],
        enable_seo_audit=False, enable_owner_enrichment=False,
        enable_full_audit_for_warm=True, enable_strategy_policy=False, enable_signal_filter_gate=False,
        # enable_llm_callsheet deliberately left at its default False.
    )
    result = process_discovery(req, task_id="cost-toggle-off")

    assert result.prospect_count == 1
    p = result.prospects[0]
    # Callsheet contributed 0.0 (templated path); only the audit's $0.0150 of
    # content-draft LLM spend is on the prospect.
    assert abs(p.llm_cost_usd - 0.0150) < 1e-9
    # The templated callsheet still ran (deterministic content present).
    assert p.callsheet_opener


# ===========================================================================
# Part C — best-effort: a cost-capture failure must not break discovery
# ===========================================================================

def test_callsheet_failure_leaves_cost_zero_discovery_completes(tmp_path, monkeypatch):
    """A callsheet generation crash -> templated fallback, llm_cost_usd 0.0,
    and the discovery run still completes."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [ProspectRecord(
            prospect_id="pr_boom", company_name="Boom Co",
            website_url="https://boom.example", industry="hvac",
            zipcode=kw["zipcode"],
        )],
    )

    # build_call_sheet_smart_costed itself blows up — the Step 3 try/except
    # must catch it, fall back to the templated callsheet, keep cost at 0.0.
    def _boom(p):
        raise RuntimeError("callsheet subsystem catastrophically unavailable")

    import backend.prospecting.service as svc_mod
    monkeypatch.setattr(svc_mod, "build_call_sheet_smart_costed", _boom)

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        zipcodes=["95993"], industries=["hvac"],
        enable_seo_audit=False, enable_owner_enrichment=False,
        enable_full_audit_for_warm=False, enable_strategy_policy=False, enable_signal_filter_gate=False,
        # build_call_sheet_smart_costed only runs on the toggle-on path, so the
        # Step 3 try/except fallback this test asserts on needs the toggle on.
        enable_llm_callsheet=True,
    )
    result = process_discovery(req, task_id="cost-boom")

    # Run still completed; prospect carries no cost + a templated callsheet.
    assert result.prospect_count == 1
    p = result.prospects[0]
    assert p.llm_cost_usd == 0.0
    assert p.callsheet_opener           # templated fallback fired
    assert p.company_name == "Boom Co"


def test_cost_capture_pricing_failure_is_best_effort(monkeypatch):
    """If pricing raises inside the callsheet path, the cost degrades to 0.0
    and the callsheet still returns usable content."""
    import backend.prospecting.callsheet as cs
    from backend.prospecting.models import ProspectRecord

    def _fake_messages(*, workcell, api_key, prompt, **kw):
        import json as _json
        return _json.dumps({"pitch": "p", "opener": "o", "voicemail": "v"}), {
            "input_tokens": 100, "output_tokens": 50,
        }
    monkeypatch.setattr(cs, "anthropic_messages", _fake_messages)

    # cost_from_usage raising is swallowed by _price_callsheet_usage's guard.
    import backend.common.llm_pricing as pricing
    def _boom(*a, **k):
        raise RuntimeError("pricing table corrupt")
    monkeypatch.setattr(pricing, "cost_from_usage", _boom)

    sheet, cost = cs.build_call_sheet_with_llm_costed(
        ProspectRecord(company_name="X", industry="hvac"),
        anthropic_api_key="sk-x",
    )
    assert cost == 0.0                 # pricing failure -> neutral 0.0
    assert sheet.callsheet_pitch == "p"  # callsheet content still delivered


# ===========================================================================
# Part D — threading: Opportunity stores it, dispatch carries it, RewardSignal
# ===========================================================================

class _FakeTable:
    """Minimal in-memory DDB shim — get/put + naive single-attr scan."""

    def __init__(self):
        self.items: dict[tuple, dict[str, Any]] = {}
        self.pk_attr = None

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


def _stub_strategy_settings(monkeypatch, *, strategy_url="http://strategy.local",
                            hmac="secret"):
    class _S:
        gateway_urls = {"strategy": strategy_url}
        # FIN-10 hard close-amount cap: advance_opportunity reads this on every
        # closed_won transition. 0.0 disables the cap (the code gates on
        # cap > 0) so these dispatch-focused tests exercise the terminal-close
        # strategy dispatch without tripping the anti-fraud ceiling, which is
        # owned by its own test (test_crm_service.test_advance_closed_won_over_cap).
        crm_max_close_amount_usd = 0.0
    s = _S()
    s.shared_hmac_key = hmac
    import backend.crm.service as svc
    monkeypatch.setattr(svc, "get_settings", lambda: s)


class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.text = ""


def _capture_strategy_posts(monkeypatch):
    posts: list[tuple] = []
    import backend.crm.service as svc

    def _fake_post(base_url, path, payload, **kw):
        posts.append((base_url, path, payload))
        return _Resp(200)

    monkeypatch.setattr(svc, "signed_post_json_sync", _fake_post)
    return posts


def test_opportunity_carries_token_cost_field():
    """Opportunity + CreateOpportunityRequest gain token_cost_usd; default 0.0."""
    from backend.crm.models import CreateOpportunityRequest, Opportunity
    opp = Opportunity(opportunity_id="op_x", token_cost_usd=0.042)
    assert opp.token_cost_usd == 0.042
    assert Opportunity(opportunity_id="op_old").token_cost_usd == 0.0

    req = CreateOpportunityRequest(prospect_id="pr_x", token_cost_usd=0.042)
    assert req.token_cost_usd == 0.042
    assert CreateOpportunityRequest(prospect_id="pr_old").token_cost_usd == 0.0


def test_create_opportunity_persists_token_cost(tmp_path, monkeypatch):
    """crm.service.create_opportunity stores token_cost_usd onto the row."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    _patch_tables(monkeypatch)

    from backend.crm.models import CreateOpportunityRequest
    from backend.crm.service import create_opportunity, get_opportunity
    result = create_opportunity(CreateOpportunityRequest(
        prospect_id="pr_acme", industry="hvac",
        policy_family="fast_quote_mode", token_cost_usd=0.0177,
    ))
    assert result.status == "created"

    opp = get_opportunity(result.opportunity_id)
    assert opp is not None
    assert abs(opp.token_cost_usd - 0.0177) < 1e-9


def test_log_call_booked_forwards_token_cost(tmp_path, monkeypatch):
    """A booked log_call threads token_cost_usd into the CreateOpportunityRequest."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.crm import service as crm_service
    from backend.crm.models import CreateOpportunityResult
    captured: dict = {}

    def _create_opportunity(req):
        captured["request"] = req
        return CreateOpportunityResult(
            status="created", opportunity_id="op_b",
            ts="2026-05-20T00:00:00Z", error=None,
        )

    monkeypatch.setattr(crm_service, "upsert_conversation", lambda c: True)
    monkeypatch.setattr(crm_service, "upsert_call_state", lambda s: True)
    monkeypatch.setattr(crm_service, "get_call_state", lambda pid: None)
    monkeypatch.setattr(crm_service, "create_opportunity", _create_opportunity)

    from backend.crm.log_call import log_call
    result = log_call(
        prospect_id="pr_acme", company="Acme HVAC", outcome="booked",
        industry="hvac", policy_family="fast_quote_mode",
        token_cost_usd=0.0291,
    )
    assert result["ok"] is True
    assert abs(captured["request"].token_cost_usd - 0.0291) < 1e-9


def test_terminal_close_dispatch_payload_carries_token_cost(tmp_path, monkeypatch):
    """A terminal transition includes token_cost_usd in the strategy payload."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_w")] = {
        "opportunity_id": "op_w", "prospect_id": "pr_acme",
        "stage": "negotiation", "deal_size_usd": 15000.0,
        "close_probability": 0.1, "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
        "industry": "hvac", "policy_family": "fast_quote_mode",
        "token_cost_usd": 0.0314,
    }
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity
    result = advance_opportunity(AdvanceOpportunityRequest(
        opportunity_id="op_w", target_stage="closed_won", won_amount_usd=20000.0,
    ))
    assert result.status == "advanced"
    assert len(posts) == 1
    _base, path, payload = posts[0]
    assert path == "/strategy/record-outcome"
    assert abs(payload["token_cost_usd"] - 0.0314) < 1e-9


def test_terminal_close_no_token_cost_defaults_zero(tmp_path, monkeypatch):
    """A pre-this-feature Opportunity (no token_cost_usd) dispatches 0.0."""
    monkeypatch.setenv("SAMUS_CRM_AUDIT_PATH", str(tmp_path / "crm_audit.jsonl"))
    tables = _patch_tables(monkeypatch)
    tables["_opportunities_table"].items[("opportunity_id", "op_old")] = {
        "opportunity_id": "op_old", "prospect_id": "pr_old",
        "stage": "proposal", "deal_size_usd": 9000.0,
        "close_probability": 0.1, "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
        "industry": "dentist", "policy_family": "reputation_repair",
    }
    _stub_strategy_settings(monkeypatch)
    posts = _capture_strategy_posts(monkeypatch)

    from backend.crm.models import AdvanceOpportunityRequest
    from backend.crm.service import advance_opportunity
    advance_opportunity(AdvanceOpportunityRequest(
        opportunity_id="op_old", target_stage="closed_lost", lost_reason="x",
    ))
    assert posts[0][2]["token_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Part D — strategy.record_outcome lands token_cost_usd on the RewardSignal
# ---------------------------------------------------------------------------

def _capture_bandit(monkeypatch):
    calls: list[dict] = []
    import backend.strategy.service as svc

    def _fake_update(industry, policy_family, outcome, *, reward_signal=None):
        calls.append({
            "industry": industry, "policy_family": policy_family,
            "outcome": outcome, "reward_signal": reward_signal,
        })

    monkeypatch.setattr(svc.portfolio_manager, "update_policy_bandit", _fake_update)
    return calls


async def _record(req):
    from backend.strategy.service import record_outcome
    return await record_outcome(req)


def test_record_outcome_request_carries_token_cost():
    """RecordOutcomeRequest gains token_cost_usd, optional/defaulted."""
    from backend.strategy.models import RecordOutcomeRequest
    req = RecordOutcomeRequest(
        prospect_id="pr_x", won=True, industry="hvac",
        policy_family="fast_quote_mode", token_cost_usd=0.05,
    )
    assert req.token_cost_usd == 0.05
    assert RecordOutcomeRequest(prospect_id="pr_y", won=False).token_cost_usd == 0.0


def test_record_outcome_real_cost_lands_on_reward_signal(monkeypatch):
    """A positive token_cost_usd is passed straight onto RewardSignal."""
    import asyncio
    calls = _capture_bandit(monkeypatch)

    from backend.strategy.models import RecordOutcomeRequest
    req = RecordOutcomeRequest(
        prospect_id="pr_acme", won=True, industry="hvac",
        policy_family="fast_quote_mode", outcome=1.0, seo_score=60,
        owner_email=True, token_cost_usd=0.0314,
    )
    asyncio.run(_record(req))

    sig = calls[0]["reward_signal"]
    assert sig is not None
    assert abs(sig.token_cost_usd - 0.0314) < 1e-9


def test_record_outcome_zero_cost_keeps_reward_signal_neutral_default(monkeypatch):
    """token_cost_usd 0.0 means 'no provenance' — RewardSignal keeps its
    documented neutral default (0.01), not a literal zero."""
    import asyncio
    calls = _capture_bandit(monkeypatch)

    from backend.strategy.models import RecordOutcomeRequest
    from backend.strategy.reward_density import RewardSignal
    req = RecordOutcomeRequest(
        prospect_id="pr_old", won=True, industry="hvac",
        policy_family="fast_quote_mode", outcome=1.0,
        # token_cost_usd left at its 0.0 default — no discovery-cost provenance.
    )
    asyncio.run(_record(req))

    sig = calls[0]["reward_signal"]
    # The neutral default is preserved — 0.0 must not override it.
    neutral_default = RewardSignal(
        outcome=1.0, owner_email=False, social_facebook=False,
        social_instagram=False, seo_score=0.0, contactability=0.5,
        infrastructure_health=0.5,
    ).token_cost_usd
    assert sig.token_cost_usd == neutral_default


def test_record_outcome_cost_changes_efficiency_weighted_reward(monkeypatch):
    """A cheap win and an expensive win produce different reward density —
    proving token_cost_usd actually reaches compute_reward_density."""
    import asyncio
    from backend.strategy import portfolio_manager as pm
    from backend.strategy.models import RecordOutcomeRequest

    # Cheap win.
    pm.reset_bandit()
    asyncio.run(_record(RecordOutcomeRequest(
        prospect_id="pr_cheap", won=True, industry="hvac",
        policy_family="fast_quote_mode", outcome=1.0, token_cost_usd=0.01,
    )))
    cheap = pm.get_policy_bandit_stats("hvac")["hvac::fast_quote_mode"]["wins"]

    # Expensive win — same arm, fresh bandit.
    pm.reset_bandit()
    asyncio.run(_record(RecordOutcomeRequest(
        prospect_id="pr_pricey", won=True, industry="hvac",
        policy_family="fast_quote_mode", outcome=1.0, token_cost_usd=5.00,
    )))
    pricey = pm.get_policy_bandit_stats("hvac")["hvac::fast_quote_mode"]["wins"]

    # efficiency_weight = (close_prob / token_cost) * 0.25 — a higher cost
    # shrinks the efficiency term, so the cheap win earns the bigger reward.
    assert cheap > pricey
