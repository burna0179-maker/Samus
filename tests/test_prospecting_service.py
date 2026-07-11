"""End-to-end service test: mocked place_search -> CSV path returned."""

from __future__ import annotations


def _fake_discover_factory(prospects_per_zip):
    def _fake(*, zipcode, industries, max_results_per_zip, must_have_website):
        return list(prospects_per_zip.get(zipcode, []))

    return _fake


def test_process_discovery_end_to_end(tmp_path, monkeypatch):
    # Redirect artifact root + audit ledger
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # Reset the in-process idempotency cache so the test is deterministic
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_test1",
                account_id="acct_test1",
                company_name="Nav Accounts",
                phone="(530) 777-3265",
                website_url="https://navaccounts.com/",
                city="Yuba City",
                state="CA",
                zipcode="95993",
                industry="finance",
                review_rating="4.8",
                review_count="23",
            ),
            ProspectRecord(
                prospect_id="pr_test2",
                account_id="acct_test2",
                company_name="Diamond Tax & Financial",
                website_url="https://diamondtaxfin.com/",
                city="Yuba City",
                state="CA",
                zipcode="95993",
                industry="finance",
                review_rating="3.6",
                review_count="8",
            ),
        ],
    }
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        _fake_discover_factory(prospects),
    )

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        campaign_name="test_campaign",
        zipcodes=["95993"],
        industries=["finance"],
        max_results_per_zip=10,
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
        # This test predates the signal_filter admission gate and uses
        # sparse fixtures (no website_status / seo_score) that the gate
        # would reject — disable it; the gate has its own test coverage in
        # test_prospecting_signal_gate.py.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="t-test-1")
    assert result.prospect_count == 2
    assert result.cache_hit is False
    assert "call_list_" in result.csv_path
    assert result.csv_path.endswith(".csv")

    # Both prospects have callsheet + score
    for p in result.prospects:
        assert p.lead_score > 0
        assert p.call_priority in ("hot", "warm", "low")
        assert p.callsheet_opener
        assert p.callsheet_pitch

    # CSV file actually written
    from pathlib import Path

    assert Path(result.csv_path).exists()


def test_process_discovery_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    counter = {"calls": 0}

    def fake_discover(*, zipcode, **_):
        counter["calls"] += 1
        return [
            ProspectRecord(
                prospect_id="pr_a",
                company_name="A",
                website_url="https://a.example",
                industry="finance",
                zipcode=zipcode,
            )
        ]

    monkeypatch.setattr("backend.prospecting.service.discover_for_zipcode", fake_discover)

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["finance"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
    )

    r1 = process_discovery(req, task_id="cache-test")
    r2 = process_discovery(req, task_id="cache-test")
    assert counter["calls"] == 1  # second run skipped Places call
    assert r1.cache_hit is False
    assert r2.cache_hit is True
    assert r1.csv_path == r2.csv_path


def test_full_audit_fires_for_warm_skips_low(tmp_path, monkeypatch):
    """Full audit_and_report fires for warm/hot prospects only — never for low."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    # Three prospects with explicit priorities pre-set so the test doesn't
    # depend on the scorer's tier thresholds.
    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_hot1",
                company_name="Hot Realty",
                website_url="https://hot.example/",
                phone="(555) 111",
                city="Yuba City",
                state="CA",
                zipcode="95993",
                industry="real estate agency",
                review_rating="5.0",
                review_count="100",
                call_priority="hot",
                lead_score=85,
            ),
            ProspectRecord(
                prospect_id="pr_warm1",
                company_name="Warm Dental",
                website_url="https://warm.example/",
                phone="(555) 222",
                city="Yuba City",
                state="CA",
                zipcode="95993",
                industry="dentist",
                review_rating="4.5",
                review_count="40",
                call_priority="warm",
                lead_score=55,
            ),
            ProspectRecord(
                prospect_id="pr_cold1",
                company_name="Cold HVAC",
                website_url="https://cold.example/",
                phone="(555) 333",
                city="Yuba City",
                state="CA",
                zipcode="95993",
                industry="hvac contractor",
                review_rating="3.0",
                review_count="5",
                call_priority="low",
                lead_score=22,
            ),
        ],
    }

    # The scorer overwrites lead_score/call_priority via score_prospect +
    # classify_priority, so monkeypatch those to preserve our explicit
    # priorities. Returning the existing values keeps the test focused on
    # the warm-gating logic.
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: list(prospects.get(kw["zipcode"], [])),
    )
    monkeypatch.setattr(
        "backend.prospecting.service.score_prospect",
        lambda p: int(p.lead_score),
    )
    monkeypatch.setattr(
        "backend.prospecting.service.classify_priority",
        lambda s: "hot" if s >= 75 else "warm" if s >= 50 else "low",
    )

    audit_calls: list[str] = []

    def _fake_audit_and_report(req, *, target_keywords=None, customer_label=None):
        audit_calls.append(req.url)
        return {
            "audit": {
                "url": req.url,
                "seo_score": 60,
                "issues": [],
                "findings": {"security": {"grade": "D"}},
            },
            "optimize": {"recommendations": []},
            "content": {"drafts": {}},
            "report_path": f"/tmp/{customer_label or 'x'}/seo_report.md",
            "customer_slug": (customer_label or "x").lower().replace(" ", "_"),
        }

    monkeypatch.setattr(
        "backend.seo.service.audit_and_report",
        _fake_audit_and_report,
    )

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["real estate agency"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=True,
        # Predates the signal_filter gate; sparse fixtures would be rejected.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="warm-audit-test")

    # audit_and_report fired exactly twice — once for hot, once for warm,
    # never for low.
    assert sorted(audit_calls) == sorted(
        [
            "https://hot.example/",
            "https://warm.example/",
        ]
    )

    by_id = {p.prospect_id: p for p in result.prospects}
    assert by_id["pr_hot1"].seo_report_path.endswith("seo_report.md")
    assert by_id["pr_warm1"].seo_report_path.endswith("seo_report.md")
    assert by_id["pr_cold1"].seo_report_path == ""

    # Step 2.7 lifts the security grade from the warm/hot audit onto the
    # prospect; the low-priority prospect is never audited, so it has none.
    assert by_id["pr_hot1"].security_grade == "D"
    assert by_id["pr_warm1"].security_grade == "D"
    assert by_id["pr_cold1"].security_grade == ""


def test_website_status_recorded_in_enrichment_step(tmp_path, monkeypatch):
    """Step 2.5 records website_status from the homepage fetch — live for a
    reachable site, the dead-class for an unreachable one, no_website when the
    prospect has no URL at all."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_live",
                company_name="Live Co",
                website_url="https://live.example/",
                industry="finance",
                zipcode="95993",
            ),
            ProspectRecord(
                prospect_id="pr_dead",
                company_name="Dead Co",
                website_url="http://dead.example/",
                industry="finance",
                zipcode="95993",
            ),
            ProspectRecord(
                prospect_id="pr_nosite",
                company_name="No Site Co",
                website_url="",
                industry="finance",
                zipcode="95993",
            ),
        ],
    }
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: list(prospects.get(kw["zipcode"], [])),
    )

    def _fake_fetch(url):
        if "dead" in url:
            return {
                "final_url": url,
                "status_code": 0,
                "html": None,
                "fetch_error": "ConnectError: [Errno 11001] getaddrinfo failed",
            }
        return {
            "final_url": url,
            "status_code": 200,
            "html": "<html><title>Live</title><h1>Live Co</h1></html>",
            "fetch_error": None,
        }

    monkeypatch.setattr("backend.prospecting.crawler.fetch_homepage", _fake_fetch)

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["finance"],
        must_have_website=False,
        enable_seo_audit=True,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=False,
        # This test asserts website_status for every input prospect — keep
        # the signal_filter gate off so a low-signal prospect is not dropped
        # before the assertions can see its status.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="website-status-test")
    by_id = {p.prospect_id: p for p in result.prospects}
    assert by_id["pr_live"].website_status == "live"
    assert by_id["pr_dead"].website_status == "domain_unresolved"
    assert by_id["pr_nosite"].website_status == "no_website"


def test_full_audit_failure_does_not_tank_run(tmp_path, monkeypatch):
    """One prospect's audit raising should leave the prospect record intact
    and let the rest of the run complete."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE)

    from backend.prospecting.models import DiscoveryRequest, ProspectRecord

    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda **kw: [
            ProspectRecord(
                prospect_id="pr_warm_boom",
                company_name="Boom Realty",
                website_url="https://boom.example/",
                phone="(555) 999",
                city="X",
                state="CA",
                zipcode=kw["zipcode"],
                industry="real estate agency",
                call_priority="warm",
                lead_score=55,
            )
        ],
    )
    monkeypatch.setattr("backend.prospecting.service.score_prospect", lambda p: int(p.lead_score))
    monkeypatch.setattr("backend.prospecting.service.classify_priority", lambda s: "warm")

    def _boom(*args, **kwargs):
        raise RuntimeError("anthropic blew up")

    monkeypatch.setattr("backend.seo.service.audit_and_report", _boom)

    from backend.prospecting.service import process_discovery

    req = DiscoveryRequest(
        zipcodes=["95993"],
        industries=["real estate agency"],
        enable_seo_audit=False,
        enable_owner_enrichment=False,
        enable_full_audit_for_warm=True,
        # Predates the signal_filter gate; sparse fixture would be rejected.
        enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="boom-test")
    assert result.prospect_count == 1
    # seo_report_path stayed empty; prospect record otherwise unaffected.
    assert result.prospects[0].seo_report_path == ""
    assert result.prospects[0].company_name == "Boom Realty"
