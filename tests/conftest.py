"""Shared test fixtures.

Tests do NOT require AWS credentials; we monkeypatch the AWS factories
out of any test that touches them. Smoke tests below cover pure-Python
logic only (governance, schema, planner).
"""
from __future__ import annotations

import os
import tempfile

import pytest


# Per-workcell LLM token budget store writes to a JSON fallback when DDB is
# unreachable. In tests AWS is not reachable, so without an override the
# JSON path defaults to /opt/samus/data/llm_budgets.json — un-writable on
# the host. Point it at a per-process tempfile so the metering paths in
# prospecting/callsheet.py + seo/content.py stay quiet during tests.
_LLM_BUDGET_TMPFD, _LLM_BUDGET_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-llm-budget-", suffix=".json",
)
os.close(_LLM_BUDGET_TMPFD)
os.environ.setdefault("SAMUS_LLM_BUDGET_PATH", _LLM_BUDGET_TMPPATH)
# Force the DDB table to empty so the budget store skips DDB entirely and
# only touches the JSON tmpfile. Tests that exercise DDB explicitly set
# DDB_LLM_BUDGETS_TABLE themselves.
os.environ.setdefault("DDB_LLM_BUDGETS_TABLE", "")
# Token-cost-hardening 2026-05-18: global $-cap store also has a JSON
# fallback. Default path lives under /opt/samus/data which isn't writable
# on the host. Redirect to a per-process tmpfile so tests that touch the
# llm_client (and therefore implicitly the global store) stay clean.
_LLM_GLOBAL_TMPFD, _LLM_GLOBAL_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-llm-global-", suffix=".json",
)
os.close(_LLM_GLOBAL_TMPFD)
os.environ.setdefault("SAMUS_LLM_GLOBAL_BUDGET_PATH", _LLM_GLOBAL_TMPPATH)

# Strategy-integration build (Unit 1): the multi-armed bandit
# (backend/strategy/portfolio_manager.py) is now durable via
# backend/strategy/bandit_store.py — a DDB table with a JSON-file fallback.
# In tests AWS is unreachable, so point the fallback at a per-process tmpfile
# and force DDB off so the bandit only ever touches the JSON file. Mirrors the
# llm_budget treatment above. Dedicated tests construct their own store.
_BANDIT_TMPFD, _BANDIT_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-strategy-bandit-", suffix=".json",
)
os.close(_BANDIT_TMPFD)
os.environ.setdefault("SAMUS_STRATEGY_BANDIT_PATH", _BANDIT_TMPPATH)
os.environ.setdefault("DDB_STRATEGY_BANDIT_TABLE", "")

# Feedback metrics (2026-06-01): backend/common/feedback_store.py is now durable
# (was an in-memory dict). Its default path is state_path("feedback/metrics.json")
# under /opt/samus/data — un-writable on the host. Point it at a per-process
# tmpfile so the crm.feedback_engine + outreach.metrics delegators stay clean,
# mirroring the llm_budget / bandit treatment. Truncated per-test below.
_FEEDBACK_TMPFD, _FEEDBACK_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-feedback-", suffix=".json",
)
os.close(_FEEDBACK_TMPFD)
os.environ.setdefault("SAMUS_FEEDBACK_STORE_PATH", _FEEDBACK_TMPPATH)

# HOTL Tranche-1 (2026-07): the unified business-event ledger
# (backend/common/business_events.py) writes to
# /opt/samus/data/telemetry/business_events.jsonl by default. On this host
# that path holds a live 3k+ event ledger that would pollute ROI /
# consolidator / journey tests. Point it at a per-process tmpfile so
# every test process gets an empty ledger, mirroring the llm_budget /
# bandit / feedback treatment above. Dedicated tests that need to inspect
# the ledger contents write into (or monkeypatch) this same path.
_BUSINESS_EVENTS_TMPFD, _BUSINESS_EVENTS_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-business-events-", suffix=".jsonl",
)
os.close(_BUSINESS_EVENTS_TMPFD)
# Start empty (mkstemp created a 0-byte file, which is a valid empty ledger).
os.environ.setdefault("SAMUS_BUSINESS_EVENTS_PATH", _BUSINESS_EVENTS_TMPPATH)

# HOTL Tranche-5 (2026-07): the durable daily-counter ledger
# (backend/common/daily_counter.py) backs the constitutional send/call caps.
# Its default path lives under the state root's coordination dir; on this host
# that could accumulate real cap counts and make the caps fire (or not) based
# on machine history. Point it at a per-process tmpfile so every test starts
# with an empty tally, mirroring the send-ramp / business-events treatment.
# Dedicated cap tests override this env with their own tmp_path.
_DAILY_COUNTER_TMPFD, _DAILY_COUNTER_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-daily-counter-", suffix=".jsonl",
)
os.close(_DAILY_COUNTER_TMPFD)
os.environ.setdefault("SAMUS_DAILY_COUNTER_PATH", _DAILY_COUNTER_TMPPATH)

# HOTL Tranche-5: the simulation registry (backend/common/simulation.py) records
# dry-run predictions keyed by decision_id. Isolate + truncate per-test so a
# simulation from one test can't satisfy another test's dispatch gate, and no
# host ledger leaks in. Dedicated sim tests override this env with tmp_path.
_SIM_TMPFD, _SIM_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-simulation-", suffix=".jsonl",
)
os.close(_SIM_TMPFD)
os.environ.setdefault("SAMUS_SIMULATION_LEDGER_PATH", _SIM_TMPPATH)

# HOTL Tranche-5: the reputation store (backend/common/reputation.py) persists a
# per-workcell table. Point it at a per-process tmpfile so a host reputation.json
# never leaks into a test and per-test recomputes don't collide. Dedicated tests
# override this env with their own tmp_path.
_REPUTATION_TMPFD, _REPUTATION_TMPPATH = tempfile.mkstemp(
    prefix="samus-test-reputation-", suffix=".json",
)
os.close(_REPUTATION_TMPFD)
os.environ.setdefault("SAMUS_REPUTATION_PATH", _REPUTATION_TMPPATH)

# Force-disable auth + AWS-region warnings during tests.
os.environ.setdefault("SAMUS_AUTH_ENABLED", "false")
os.environ.setdefault("AWS_REGION", "us-west-1")
os.environ.setdefault("SAMUS_AGENT_HMAC_SECRET", "x" * 32)
# Feedback workcell SNS signature verification is strict in production. The
# generic feedback-app / handler tests use synthetic unsigned payloads, so we
# disable verification by default in the test process. The dedicated
# test_feedback_sns_signature.py test file explicitly re-enables it and posts
# real signed bodies through the route to exercise the verifier path.
os.environ.setdefault("SAMUS_FEEDBACK_VERIFY_SNS", "0")
# Voice workcell Vapi webhook verification follows the same convention as the
# feedback workcell — strict in production, opt-out for the generic test
# suite. test_voice_app.py flips this back on per-test to exercise the
# signature-gate paths.
os.environ.setdefault("SAMUS_VOICE_VERIFY_WEBHOOK", "0")
# Inter-workcell HMAC middleware (VerifyHMACMiddleware) went default-on in
# create_base_app as of 2026-05-19. The pre-existing test suite constructs
# unsigned TestClient requests by the hundreds; flipping the default would
# break every workcell test. This env var opts the entire pytest process out
# so the middleware short-circuits at app construction. Production / Cloud
# Run / Compose never set this var. Dedicated middleware-behaviour tests
# (test_common_app_factory_hmac_default.py) un-set this per-test to prove
# the default-on contract.
os.environ.setdefault("SAMUS_DISABLE_HMAC_MIDDLEWARE", "1")
# SEO passive security audit (backend/seo/security_audit.py) issues real DNS
# lookups + a TLS handshake + a few benign HTTP probes. The generic SEO test
# suite mocks only the page-fetch httpx surface, so leaving the security audit
# enabled would let those tests reach the real internet (and run slowly).
# Default it OFF for the pytest process; backend/seo/test_seo_security_audit.py
# re-enables it per-test with every network call mocked.
os.environ.setdefault("SAMUS_SEO_SECURITY_AUDIT_ENABLED", "false")
# Intake-hardening: the public /intake/onboarding rate limiter is a
# DynamoDB-backed counter. Production / Cloud Run enable it; the generic
# intake test suite posts unthrottled by the dozen, so the pytest process
# opts out by default. The dedicated test_intake_rate_limit.py file
# re-enables it per-test to exercise the limiter contract.
os.environ.setdefault("SAMUS_INTAKE_RATE_LIMIT_ENABLED", "0")
# auth+HTTP-surface hardening (finding M7, 2026-05-20): the in-process
# inter-workcell rate limiter (backend/common/rate_limit.py) guards the
# LLM-backed / outbound-action routes (voice, outreach, seo, proposal,
# finance meter-event). Production / Cloud Run / Compose enable it; the
# generic workcell test suite posts unthrottled by the dozen, so the pytest
# process opts out by default. The dedicated test_common_rate_limit.py file
# re-enables it per-test to exercise the limiter contract.
os.environ.setdefault("SAMUS_RATE_LIMIT_ENABLED", "0")


def _reset_llm_singletons() -> None:
    """token-cost-hardening 2026-05-18: clear LLM store singletons.

    Each test must see a fresh global $-cap store + per-workcell store
    or the day-counter / breaker state leaks between tests. The strategy
    bandit store (Unit 1) is reset here too — its singleton caches the
    resolved table name / JSON path and must be rebuilt per test.
    """
    try:
        from backend.common.llm_budget import reset_store
        reset_store()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.common.llm_global_budget import reset_global_store
        reset_global_store()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.strategy.bandit_store import reset_store as _reset_bandit_store
        _reset_bandit_store()
    except Exception:  # noqa: BLE001
        pass


def _reset_voice_ledger_singletons() -> None:
    """Clear the module-level voice audit/events JsonlLedger singletons.

    voice/service.py caches ``_audit_ledger_instance`` / ``_events_ledger_instance``
    (one shared lock per file path, for write concurrency). Tests set
    ``SAMUS_VOICE_AUDIT_PATH`` / ``SAMUS_VOICE_EVENTS_PATH`` to a per-test
    tmp_path; without resetting the singletons the FIRST test's path is cached
    and later tests write to the wrong file. Reset per test so each sees a fresh
    ledger bound to its own env path.
    """
    try:
        from backend.voice import service as _voice_service
        _voice_service._audit_ledger_instance = None
        _voice_service._events_ledger_instance = None
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    from backend.common.settings import reload_settings
    reload_settings()
    _reset_llm_singletons()
    _reset_voice_ledger_singletons()
    yield
    reload_settings()
    _reset_llm_singletons()
    _reset_voice_ledger_singletons()
    # Truncate the JSON-backed stores so per-test state doesn't bleed
    # (efficiency EMAs, daily counters, circuit-breaker state, $-cap counters,
    # and the strategy bandit's wins/trials arms).
    for path in (_LLM_BUDGET_TMPPATH, _LLM_GLOBAL_TMPPATH, _BANDIT_TMPPATH, _FEEDBACK_TMPPATH):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
        except OSError:
            pass
    # Business events JSONL: an empty file is a valid empty ledger.
    try:
        with open(_BUSINESS_EVENTS_TMPPATH, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass
    # Daily-counter JSONL (send/call caps): empty file == empty tally.
    try:
        with open(_DAILY_COUNTER_TMPPATH, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass
    # Simulation registry JSONL: empty file == no recorded simulations.
    try:
        with open(_SIM_TMPPATH, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass
    # Reputation store: remove so load_reputation() sees "no persisted table".
    try:
        if os.path.exists(_REPUTATION_TMPPATH):
            os.remove(_REPUTATION_TMPPATH)
    except OSError:
        pass


# Flag-store singleton isolation (authored 2026-06-12 on the unmerged
# feat/cashflow-remediation branch; re-ported to the live tree 2026-07-03 —
# the geo-ring dormancy tests already referenced this fixture by name in
# their docstrings but it never landed): any test that installs the
# process-global flag store — directly or by booting an app lifespan — can
# leak REAL persisted operator flips (flag_values.json) into later tests,
# flipping a settings-stubbed "disabled" gate to enabled. Reset both sides
# of every test.
@pytest.fixture(autouse=True)
def _reset_flag_store():
    from backend.common.flags.store import reset_default_store_for_testing

    reset_default_store_for_testing()
    yield
    reset_default_store_for_testing()


# Codex Validation Layer (chapter 12 / ADR-011): the registry parses
# docs/codex/ once at session start so integration paths that gate on the
# Codex can run. Parse failure fails the entire test session (fail-closed),
# matching production semantics.
def _load_codex_registry_for_tests() -> None:
    try:
        from backend.common.codex import REGISTRY
        if not REGISTRY.is_loaded():
            REGISTRY.load()
    except Exception as exc:  # noqa: BLE001
        import sys
        print(
            f"[conftest] Codex registry failed to load — tests that integrate "
            f"with the validator will raise CodexUnavailable: {exc}",
            file=sys.stderr,
        )


_load_codex_registry_for_tests()
