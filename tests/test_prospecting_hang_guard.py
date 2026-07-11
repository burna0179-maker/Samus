"""Regression tests for the 2026-07-01 daily-prospecting hang.

A per-prospect enrichment/audit step with an unbounded blocking network call
(root cause: ``socket.getaddrinfo`` with no timeout, before the httpx request)
stalled the whole sequential ``process_discovery`` run past the scheduled
task's 20-minute limit, so no ``call_list_*.csv`` was ever written.

These tests pin the fix's contract at the service layer, fully offline:
  * a hanging/slow enrichment is bounded by a wall-clock deadline and treated
    as a SKIP (the prospect stays on the list at its discovery-stage state);
  * the run completes and writes a NON-EMPTY call list when >= 1 prospect
    survives, even if every enrichment step failed;
  * one failing prospect never aborts the batch.

No real network: ``discover_for_zipcode`` is monkeypatched to return fixtures,
and the enrichment internals (``fetch_homepage`` etc.) are patched to hang /
raise on demand.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    store = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", store)
    import backend.prospecting.service as svc_mod
    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", store)


def _fixture_prospects():
    from backend.prospecting.models import ProspectRecord
    return [
        ProspectRecord(
            prospect_id="pr_fast", account_id="acct_fast",
            company_name="Fast Co", phone="(530) 111-1111",
            website_url="https://fast.example/",
            city="Yuba City", state="CA", zipcode="95993",
            industry="finance", review_rating="4.8", review_count="20",
        ),
        ProspectRecord(
            prospect_id="pr_slow", account_id="acct_slow",
            company_name="Slow Co", phone="(530) 222-2222",
            website_url="https://slow.example/",
            city="Yuba City", state="CA", zipcode="95993",
            industry="finance", review_rating="4.1", review_count="9",
        ),
    ]


def _base_request(**overrides):
    from backend.prospecting.models import DiscoveryRequest
    kwargs = dict(
        campaign_name="hang_guard_test",
        zipcodes=["95993"],
        industries=["finance"],
        max_results_per_zip=10,
        enable_owner_enrichment=True,
        enable_seo_audit=True,
        enable_full_audit_for_warm=False,
        enable_strategy_policy=False,
        enable_website_recheck=False,
        # The sparse fixtures would be rejected by the admission gate; it has
        # its own coverage. Disable so we exercise the enrichment path itself.
        enable_signal_filter_gate=False,
    )
    kwargs.update(overrides)
    return DiscoveryRequest(**kwargs)


def _common_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _reset_idempotency(monkeypatch)
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        lambda *, zipcode, industries, max_results_per_zip, must_have_website:
            list(_fixture_prospects()) if zipcode == "95993" else [],
    )


def test_hanging_enrichment_is_bounded_and_skipped(tmp_path, monkeypatch):
    """A prospect whose homepage fetch hangs forever is bounded by the
    enrichment deadline and skipped; the fast prospect is enriched normally
    and the run finishes writing a non-empty list — quickly."""
    _common_env(monkeypatch, tmp_path)

    # Tiny deadline so the test is fast; a real hang would blow past it.
    monkeypatch.setattr(
        "backend.prospecting.service._ENRICH_DEADLINE_S", 0.3,
    )

    released = threading.Event()

    def _fake_fetch_homepage(url):
        if "slow" in url:
            # Simulate the unbounded getaddrinfo/socket hang.
            released.wait(timeout=10.0)
        return {"final_url": url, "status_code": 200,
                "html": "<title>ok</title>", "fetch_error": None}

    monkeypatch.setattr(
        "backend.prospecting.crawler.fetch_homepage", _fake_fetch_homepage,
    )
    monkeypatch.setattr(
        "backend.prospecting.crawler.classify_website", lambda page: "live",
    )
    monkeypatch.setattr(
        "backend.prospecting.seo_audit.score_seo", lambda page: (55, []),
    )
    monkeypatch.setattr(
        "backend.prospecting.enrichment.enrich_from_page_with_fallback",
        lambda page, base_url, enable_facebook=True: {"owner_email": "a@fast.example"},
    )

    from backend.prospecting.service import process_discovery

    started = time.monotonic()
    result = process_discovery(_base_request(), task_id="t-hang-1")
    elapsed = time.monotonic() - started
    released.set()  # let the abandoned worker unwind

    # The whole run is bounded — nowhere near the 10s simulated hang.
    assert elapsed < 5.0, f"run was not bounded by the deadline (took {elapsed:.2f}s)"

    # Both prospects survive (partial success > empty output).
    assert result.prospect_count == 2
    by_id = {p.prospect_id: p for p in result.prospects}
    # Fast prospect got enriched (seo_score set from the mocked score_seo).
    assert by_id["pr_fast"].website_status == "live"
    # Slow prospect was skipped mid-enrichment, kept at discovery-stage state
    # (no website_status applied by the enrichment step).
    assert by_id["pr_slow"].website_status in ("", None)

    # A non-empty call list was actually written to disk.
    assert Path(result.csv_path).exists()
    assert Path(result.csv_path).stat().st_size > 0
    assert result.txt_path and Path(result.txt_path).exists()


def test_one_failing_prospect_does_not_abort_batch(tmp_path, monkeypatch):
    """A prospect whose enrichment RAISES is skipped; the batch continues and
    a non-empty list is still produced."""
    _common_env(monkeypatch, tmp_path)

    def _fake_fetch_homepage(url):
        if "slow" in url:
            raise RuntimeError("boom: hostile site")
        return {"final_url": url, "status_code": 200,
                "html": "<title>ok</title>", "fetch_error": None}

    monkeypatch.setattr(
        "backend.prospecting.crawler.fetch_homepage", _fake_fetch_homepage,
    )
    monkeypatch.setattr(
        "backend.prospecting.crawler.classify_website", lambda page: "live",
    )
    monkeypatch.setattr(
        "backend.prospecting.seo_audit.score_seo", lambda page: (60, []),
    )
    monkeypatch.setattr(
        "backend.prospecting.enrichment.enrich_from_page_with_fallback",
        lambda page, base_url, enable_facebook=True: {},
    )

    from backend.prospecting.service import process_discovery
    result = process_discovery(_base_request(), task_id="t-hang-2")

    assert result.prospect_count == 2  # both retained
    by_id = {p.prospect_id: p for p in result.prospects}
    assert by_id["pr_fast"].website_status == "live"
    assert by_id["pr_slow"].website_status in ("", None)  # failed -> kept as-is
    assert Path(result.csv_path).exists()
    assert Path(result.csv_path).stat().st_size > 0


def test_all_enrichment_failing_still_writes_nonempty_list(tmp_path, monkeypatch):
    """Even if EVERY prospect's enrichment fails, discovery output is still
    persisted — the writer runs on whatever survived."""
    _common_env(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "backend.prospecting.crawler.fetch_homepage",
        lambda url: (_ for _ in ()).throw(RuntimeError("all sites down")),
    )
    monkeypatch.setattr(
        "backend.prospecting.crawler.classify_website", lambda page: "live",
    )
    monkeypatch.setattr(
        "backend.prospecting.seo_audit.score_seo", lambda page: (0, []),
    )
    monkeypatch.setattr(
        "backend.prospecting.enrichment.enrich_from_page_with_fallback",
        lambda page, base_url, enable_facebook=True: {},
    )

    from backend.prospecting.service import process_discovery
    result = process_discovery(_base_request(), task_id="t-hang-3")

    assert result.prospect_count == 2
    assert Path(result.csv_path).exists()
    assert Path(result.csv_path).stat().st_size > 0


def test_hanging_full_audit_is_bounded_and_skipped(tmp_path, monkeypatch):
    """A warm/hot prospect whose deep audit hangs is bounded by the audit
    deadline and skipped (kept on the list, no report), run still completes."""
    _common_env(monkeypatch, tmp_path)

    # Enrichment is cheap here; the hang is in the Step 2.7 deep audit.
    monkeypatch.setattr(
        "backend.prospecting.crawler.fetch_homepage",
        lambda url: {"final_url": url, "status_code": 200,
                     "html": "<title>ok</title>", "fetch_error": None},
    )
    monkeypatch.setattr(
        "backend.prospecting.crawler.classify_website", lambda page: "live",
    )
    monkeypatch.setattr(
        "backend.prospecting.seo_audit.score_seo", lambda page: (30, []),
    )
    monkeypatch.setattr(
        "backend.prospecting.enrichment.enrich_from_page_with_fallback",
        lambda page, base_url, enable_facebook=True: {"owner_email": "o@x.example"},
    )

    monkeypatch.setattr(
        "backend.prospecting.service._AUDIT_DEADLINE_S", 0.3,
    )

    released = threading.Event()

    def _hanging_audit(req, *, target_keywords=None, customer_label=None):
        released.wait(timeout=10.0)
        return {"report_path": "/tmp/never.md", "audit": {}, "content": {}}

    monkeypatch.setattr(
        "backend.seo.service.audit_and_report", _hanging_audit,
    )

    from backend.prospecting.service import process_discovery
    started = time.monotonic()
    result = process_discovery(
        _base_request(enable_full_audit_for_warm=True), task_id="t-hang-4",
    )
    elapsed = time.monotonic() - started
    released.set()

    assert elapsed < 6.0, f"audit hang not bounded (took {elapsed:.2f}s)"
    assert result.prospect_count == 2
    # No report path landed on the (hung) audit prospects.
    for p in result.prospects:
        assert not p.seo_report_path
    assert Path(result.csv_path).exists()
    assert Path(result.csv_path).stat().st_size > 0
