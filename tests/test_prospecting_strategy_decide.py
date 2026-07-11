"""process_discovery Step 2.6 — the strategy "decide" step.

Covers: policy_family stamped on discovered prospects when the toggle is on;
exactly one select_best_policy call per distinct industry (not per prospect);
best-effort degradation (strategy raising -> policy_family="" + pipeline still
completes); toggle off -> no strategy call + policy_family="".

select_best_policy is monkeypatched in every test so the suite stays offline
and deterministic — it never touches the real DDB-backed bandit.
"""

from __future__ import annotations


def _wire_isolated_idempotency(monkeypatch):
    """Give each test its own idempotency store so runs don't cache-collide."""
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod

    monkeypatch.setattr(
        svc_mod,
        "GLOBAL_IDEMPOTENCY_STORE",
        idem_mod.GLOBAL_IDEMPOTENCY_STORE,
    )


def test_policy_family_stamped_when_toggle_on(tmp_path, monkeypatch):
    """With enable_strategy_policy on, every discovered prospect carries the
    bandit-chosen policy family for its industry."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_a",
                company_name="A Co",
                website_url="https://a.example",
                industry="hvac",
                zipcode="95993",
            ),
            ProspectRecord(
                prospect_id="pr_b",
                company_name="B Co",
                website_url="https://b.example",
                industry="hvac",
                zipcode="95993",
            ),
        ],
    }
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: list(prospects.get(kw["zipcode"], [])),
    )
    # Deterministic, offline strategy stub — never touches the real bandit.
    monkeypatch.setattr(
        "backend.strategy.portfolio_manager.select_best_policy",
        lambda industry: f"policy_for::{industry}",
    )

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["hvac"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
        enable_strategy_policy=True,
        # Predates the signal_filter gate; sparse fixtures would be rejected.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="decide-on")

    assert result.prospect_count == 2
    for p in result.prospects:
        assert p.policy_family == "policy_for::hvac"


def test_select_best_policy_called_once_per_distinct_industry(tmp_path, monkeypatch):
    """select_best_policy is called once per distinct industry — not once per
    prospect — and the answer is reused across same-industry prospects."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    # 4 prospects across 2 industries (3x hvac, 1x dentist).
    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_1",
                company_name="HVAC One",
                website_url="https://1.example",
                industry="hvac",
                zipcode="95993",
            ),
            ProspectRecord(
                prospect_id="pr_2",
                company_name="HVAC Two",
                website_url="https://2.example",
                industry="hvac",
                zipcode="95993",
            ),
            ProspectRecord(
                prospect_id="pr_3",
                company_name="HVAC Three",
                website_url="https://3.example",
                industry="hvac",
                zipcode="95993",
            ),
            ProspectRecord(
                prospect_id="pr_4",
                company_name="Dental Four",
                website_url="https://4.example",
                industry="dentist",
                zipcode="95993",
            ),
        ],
    }
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: list(prospects.get(kw["zipcode"], [])),
    )

    industry_calls: list[str] = []

    def _counting_select(industry):
        industry_calls.append(industry)
        return f"chosen::{industry}"

    monkeypatch.setattr(
        "backend.strategy.portfolio_manager.select_best_policy",
        _counting_select,
    )

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["hvac", "dentist"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
        enable_strategy_policy=True,
        # Predates the signal_filter gate; sparse fixtures would be rejected.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="decide-dedup")

    # 4 prospects, 2 distinct industries -> exactly 2 calls.
    assert sorted(industry_calls) == ["dentist", "hvac"]
    by_id = {p.prospect_id: p for p in result.prospects}
    assert by_id["pr_1"].policy_family == "chosen::hvac"
    assert by_id["pr_2"].policy_family == "chosen::hvac"
    assert by_id["pr_3"].policy_family == "chosen::hvac"
    assert by_id["pr_4"].policy_family == "chosen::dentist"


def test_strategy_failure_degrades_to_empty_policy(tmp_path, monkeypatch):
    """If select_best_policy raises, policy_family stays "" and the discovery
    run completes unaffected — strategy being down never tanks prospecting."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [
            ProspectRecord(
                prospect_id="pr_boom",
                company_name="Boom Co",
                website_url="https://boom.example",
                industry="hvac",
                zipcode=kw["zipcode"],
            )
        ],
    )

    def _boom(industry):
        raise RuntimeError("bandit store catastrophically unavailable")

    monkeypatch.setattr(
        "backend.strategy.portfolio_manager.select_best_policy",
        _boom,
    )

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["hvac"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
        enable_strategy_policy=True,
        # Predates the signal_filter gate; sparse fixtures would be rejected.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="decide-boom")

    # Run still completed; the prospect just carries no policy family.
    assert result.prospect_count == 1
    assert result.prospects[0].policy_family == ""
    assert result.prospects[0].company_name == "Boom Co"
    # And the rest of the pipeline ran — callsheet + score still present.
    assert result.prospects[0].callsheet_opener
    assert result.prospects[0].lead_score > 0


def test_toggle_off_skips_strategy_entirely(tmp_path, monkeypatch):
    """With enable_strategy_policy off, select_best_policy is never called and
    policy_family stays "" on every prospect."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _wire_isolated_idempotency(monkeypatch)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [
            ProspectRecord(
                prospect_id="pr_off",
                company_name="Off Co",
                website_url="https://off.example",
                industry="hvac",
                zipcode=kw["zipcode"],
            )
        ],
    )

    calls: list[str] = []

    def _should_not_run(industry):  # pragma: no cover - asserted not called
        calls.append(industry)
        return "should_not_appear"

    monkeypatch.setattr(
        "backend.strategy.portfolio_manager.select_best_policy",
        _should_not_run,
    )

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["hvac"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
        enable_strategy_policy=False,
        # Predates the signal_filter gate; sparse fixtures would be rejected.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="decide-off")

    assert calls == []  # strategy never consulted
    assert result.prospect_count == 1
    assert result.prospects[0].policy_family == ""
