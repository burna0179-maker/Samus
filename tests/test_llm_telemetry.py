"""Cognitive LLM call-trace -- per-reasoning-call routing + budget telemetry.

Covers the standalone telemetry module (record shape, control classification,
fail-soft, counters) and its wiring into ``llm_client.anthropic_messages`` so
every reasoning call emits a reconstructable routed/denied trace without
changing the call's return/exception contract.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.common import llm_client, llm_telemetry
from backend.common import metrics
from backend.common.llm_budget import LlmBudgetStore


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_llm_client.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _telemetry_to_tmp(tmp_path, monkeypatch):
    """Point the reasoning-call ledger at a tmp file for every test."""
    monkeypatch.setenv(
        "SAMUS_LLM_TELEMETRY_PATH",
        str(tmp_path / "llm_calls.jsonl"),
    )
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")


def _store(tmp_path, **kwargs) -> LlmBudgetStore:
    base = dict(
        base_token_budget=10_000,
        ema_alpha=0.5,
        floor_pct=0.10,
        ddb_table=None,
        json_path=str(tmp_path / "b.json"),
    )
    base.update(kwargs)
    return LlmBudgetStore(**base)


def _patch_httpx(
    monkeypatch, *, status: int = 200, body: dict | None = None, raise_exc: Exception | None = None
):
    class _Resp:
        def __init__(self):
            self.status_code = status
            self._body = body or {}
            self.text = json.dumps(self._body)

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)


def _ok_payload(
    in_tokens: int = 100, out_tokens: int = 50, text: str = "hello world"
) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens},
    }


# ---------------------------------------------------------------------------
# Module-direct: record_llm_call + read_calls
# ---------------------------------------------------------------------------


class TestRecordLlmCall:
    def test_routed_record_is_reconstructable(self):
        llm_telemetry.record_llm_call(
            workcell="cognition",
            backend=llm_telemetry.BACKEND_LOCAL,
            decision=llm_telemetry.ROUTED,
            model="local",
            est_tokens=1775,
            used_tokens=150,
            quota_tokens=200_000,
            actual_cost_usd=0.0,
            outcome="success",
            latency_ms=42.0,
        )
        rows = llm_telemetry.read_calls(workcell="cognition")
        assert len(rows) == 1
        rec = rows[0]
        assert rec["decision"] == "routed"
        assert rec["backend"] == "local"
        assert rec["outcome"] == "success"
        assert rec["used_tokens"] == 150
        assert rec["actual_cost_usd"] == 0.0
        # Every record is drillable + journey-correlatable.
        assert rec["call_id"] and rec["trace_id"] and rec["ts"]

    def test_denied_record_carries_control(self):
        llm_telemetry.record_llm_call(
            workcell="cognition",
            backend=llm_telemetry.BACKEND_OPENAI,
            decision=llm_telemetry.DENIED,
            model="gpt-4.1-mini",
            control=llm_telemetry.CONTROL_WORKCELL_QUOTA,
            reason="budget_exceeded: used=199518 + req=1775 > quota=200000",
            est_tokens=1775,
            used_tokens=199_518,
            quota_tokens=200_000,
        )
        denied = llm_telemetry.read_calls(
            workcell="cognition",
            decision="denied",
        )
        assert len(denied) == 1
        assert denied[0]["control"] == "workcell_quota"
        assert denied[0]["backend"] == "openai"

    def test_read_calls_filters_by_backend(self):
        llm_telemetry.record_llm_call(
            workcell="cognition",
            backend=llm_telemetry.BACKEND_OPENAI,
            decision=llm_telemetry.ROUTED,
            outcome="success",
        )
        llm_telemetry.record_llm_call(
            workcell="cognition",
            backend=llm_telemetry.BACKEND_LOCAL,
            decision=llm_telemetry.ROUTED,
            outcome="success",
        )
        paid = llm_telemetry.read_calls(workcell="cognition", backend="openai")
        assert len(paid) == 1
        assert paid[0]["backend"] == "openai"

    def test_reason_is_bounded(self):
        rec = llm_telemetry.record_llm_call(
            workcell="x",
            backend="local",
            decision=llm_telemetry.DENIED,
            reason="z" * 5000,
        )
        assert len(rec["reason"]) <= 200


class TestClassifyDenyControl:
    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("budget_exceeded: used=9 + req=2 > quota=10", "workcell_quota"),
            ("circuit_open_until_2026-07-07T12:30:00Z", "circuit"),
            ("nonessential_frozen_until_2026-07-07T20:00:00Z", "frozen"),
            ("broker:capacity_exhausted", "broker"),
            ("", "workcell_quota"),
        ],
    )
    def test_maps_reason_to_control(self, reason, expected):
        assert llm_telemetry.classify_deny_control(reason) == expected


class TestFailSoft:
    def test_ledger_error_never_raises(self, monkeypatch):
        def _boom(**kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr(llm_telemetry, "open_ledger", _boom)
        # Must not raise, and must still return the record for inspection.
        rec = llm_telemetry.record_llm_call(
            workcell="cognition",
            backend="local",
            decision=llm_telemetry.ROUTED,
            outcome="success",
        )
        assert rec["workcell"] == "cognition"
        assert rec["decision"] == "routed"


class TestCounters:
    def test_routing_and_denial_counters_increment(self):
        routed_before = metrics.SAMUS_LLM_ROUTING_TOTAL.labels(
            workcell="countcell",
            backend="local",
        )._value.get()
        denied_before = metrics.SAMUS_LLM_DENIALS_TOTAL.labels(
            workcell="countcell",
            control="workcell_quota",
        )._value.get()

        llm_telemetry.record_llm_call(
            workcell="countcell",
            backend="local",
            decision=llm_telemetry.ROUTED,
            outcome="success",
        )
        llm_telemetry.record_llm_call(
            workcell="countcell",
            backend="local",
            decision=llm_telemetry.DENIED,
            control=llm_telemetry.CONTROL_WORKCELL_QUOTA,
        )

        assert (
            metrics.SAMUS_LLM_ROUTING_TOTAL.labels(
                workcell="countcell",
                backend="local",
            )._value.get()
            == routed_before + 1
        )
        assert (
            metrics.SAMUS_LLM_DENIALS_TOTAL.labels(
                workcell="countcell",
                control="workcell_quota",
            )._value.get()
            == denied_before + 1
        )


# ---------------------------------------------------------------------------
# Integration: llm_client.anthropic_messages emits a trace per call
# ---------------------------------------------------------------------------


class TestLlmClientEmitsTrace:
    def test_success_emits_routed_trace(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _patch_httpx(monkeypatch, body=_ok_payload(in_tokens=42, out_tokens=21))
        text, usage = llm_client.anthropic_messages(
            workcell="cognition",
            api_key="k",
            prompt="hi",
            store=store,
        )
        # Return contract unchanged.
        assert text == "hello world"
        assert usage["input_tokens"] == 42
        # A reconstructable routed trace was emitted.
        rows = llm_telemetry.read_calls(workcell="cognition", decision="routed")
        assert len(rows) == 1
        rec = rows[0]
        assert rec["outcome"] == "success"
        assert rec["backend"] == "local"  # no OPENAI_API_KEY in test env = free
        assert rec["used_tokens"] == 63  # 42 + 21
        assert rec["actual_cost_usd"] == 0.0  # local is free

    def test_over_quota_emits_denied_trace(self, tmp_path, monkeypatch):
        store = _store(tmp_path, base_token_budget=10)
        _patch_httpx(monkeypatch, body=_ok_payload())
        store.record_spend(
            "cognition",
            input_tokens=8,
            output_tokens=3,
            outcome="success",
        )
        with pytest.raises(llm_client.BudgetExceeded):
            llm_client.anthropic_messages(
                workcell="cognition",
                api_key="k",
                prompt="x" * 1000,
                store=store,
                max_tokens=500,
            )
        denied = llm_telemetry.read_calls(
            workcell="cognition",
            decision="denied",
        )
        assert len(denied) == 1
        assert denied[0]["control"] == "workcell_quota"

    def test_transport_error_emits_error_trace(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _patch_httpx(monkeypatch, raise_exc=httpx.ConnectError("down"))
        with pytest.raises(llm_client.LlmCallError):
            llm_client.anthropic_messages(
                workcell="cognition",
                api_key="k",
                prompt="hi",
                store=store,
            )
        rows = llm_telemetry.read_calls(workcell="cognition")
        assert len(rows) == 1
        assert rows[0]["decision"] == "routed"
        assert rows[0]["outcome"] == "error"
        assert "transport" in rows[0]["reason"]

    def test_http_5xx_emits_error_trace(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _patch_httpx(monkeypatch, status=502, body={"error": "upstream"})
        with pytest.raises(llm_client.LlmCallError):
            llm_client.anthropic_messages(
                workcell="cognition",
                api_key="k",
                prompt="hi",
                store=store,
            )
        rows = llm_telemetry.read_calls(workcell="cognition")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "error"
        assert rows[0]["reason"] == "http_502"

    def test_denied_trace_does_not_fire_http(self, tmp_path, monkeypatch):
        """A denied call must emit exactly one DENIED trace and zero routed."""
        store = _store(tmp_path, base_token_budget=10)
        store.record_spend(
            "cognition",
            input_tokens=8,
            output_tokens=3,
            outcome="success",
        )
        _patch_httpx(monkeypatch, body=_ok_payload())
        with pytest.raises(llm_client.BudgetExceeded):
            llm_client.anthropic_messages(
                workcell="cognition",
                api_key="k",
                prompt="x" * 1000,
                store=store,
                max_tokens=500,
            )
        rows = llm_telemetry.read_calls(workcell="cognition")
        assert len(rows) == 1
        assert rows[0]["decision"] == "denied"

    def test_broker_denied_emits_broker_trace(self, tmp_path, monkeypatch):
        """A broker denial (fail-closed) attributes to control=broker."""
        store = _store(tmp_path)
        _patch_httpx(monkeypatch, body=_ok_payload())

        def _deny(**kwargs):
            raise llm_client._broker_client.BrokerDenied(
                kind="llm_tokens",
                reason="capacity_exhausted",
                retry_after_sec=30,
            )

        monkeypatch.setattr(llm_client._broker_client, "reserve", _deny)
        with pytest.raises(llm_client.BudgetExceeded):
            llm_client.anthropic_messages(
                workcell="cognition",
                api_key="k",
                prompt="hi",
                store=store,
            )
        denied = llm_telemetry.read_calls(
            workcell="cognition",
            decision="denied",
        )
        assert len(denied) == 1
        assert denied[0]["control"] == "broker"
