"""anthropic_messages wrapper — budget enforcement + outcome accounting."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.common import llm_client
from backend.common.llm_budget import LlmBudgetStore


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
            if isinstance(self._body, Exception):
                raise self._body
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

    import backend.common.llm_client as mod

    monkeypatch.setattr(mod.httpx, "Client", _Client)


def _ok_payload(
    in_tokens: int = 100, out_tokens: int = 50, text: str = "hello world"
) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens},
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_call_returns_text_and_usage(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_httpx(monkeypatch, body=_ok_payload(in_tokens=42, out_tokens=21))
    text, usage = llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        store=store,
    )
    assert text == "hello world"
    # Control D (token-cost-hardening 2026-05-18) added cache fields to the
    # usage dict. Existing callers only care about input/output, so check
    # those explicitly. Cache fields are 0 when the beta isn't engaged.
    assert usage["input_tokens"] == 42
    assert usage["output_tokens"] == 21
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


def test_success_records_tokens_to_budget(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_httpx(monkeypatch, body=_ok_payload(in_tokens=42, out_tokens=21))
    llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        store=store,
    )
    b = store.snapshot("prospecting")
    assert b.input_tokens_today == 42
    assert b.output_tokens_today == 21
    assert b.success_count_today == 1
    assert b.error_count_today == 0


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_over_quota_raises_budget_exceeded_before_http_call(tmp_path, monkeypatch):
    """When pre-flight denies, we must not have hit Anthropic."""
    store = _store(tmp_path, base_token_budget=10)
    called = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            called["n"] += 1
            raise AssertionError("should not be called when over budget")

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    # Burn through the quota first
    store.record_spend("prospecting", input_tokens=8, output_tokens=3, outcome="success")
    with pytest.raises(llm_client.BudgetExceeded) as ei:
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="x" * 1000,
            store=store,
            max_tokens=500,
        )
    assert called["n"] == 0
    assert ei.value.decision.allowed is False


# ---------------------------------------------------------------------------
# Error paths — do NOT punish efficiency EMA
# ---------------------------------------------------------------------------


def test_transport_error_records_as_error_outcome(tmp_path, monkeypatch):
    store = _store(tmp_path, ema_alpha=0.5)
    _patch_httpx(monkeypatch, raise_exc=httpx.ConnectError("down"))
    with pytest.raises(llm_client.LlmCallError) as ei:
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="hi",
            store=store,
        )
    assert "llm_transport_error" in str(ei.value)
    b = store.snapshot("prospecting")
    assert b.error_count_today == 1
    assert b.efficiency_call_count == 0  # error doesn't count
    assert b.efficiency_ema == pytest.approx(1.0)  # untouched


def test_5xx_records_as_error_outcome(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_httpx(monkeypatch, status=502, body={"error": "upstream"})
    with pytest.raises(llm_client.LlmCallError) as ei:
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="hi",
            store=store,
        )
    assert "llm_http_502" in str(ei.value)
    b = store.snapshot("prospecting")
    assert b.error_count_today == 1
    assert b.efficiency_ema == pytest.approx(1.0)


def test_missing_content_records_as_error(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_httpx(monkeypatch, body={"usage": {"input_tokens": 1, "output_tokens": 1}})
    with pytest.raises(llm_client.LlmCallError):
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="hi",
            store=store,
        )
    b = store.snapshot("prospecting")
    assert b.error_count_today == 1
    assert b.success_count_today == 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_empty_api_key_accepted():
    """api_key is accepted for backward compat but no longer validated."""
    # Should not raise — auth is resolved from env vars, not the api_key param.
    # (Will fail on the HTTP call since there's no mock, but not on validation.)
    try:
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="",
            prompt="hi",
        )
    except llm_client.LlmCallError as exc:
        assert "api_key" not in str(exc).lower()


def test_empty_prompt_raises():
    with pytest.raises(llm_client.LlmCallError):
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="",
        )


def test_estimate_tokens_grows_with_prompt():
    short = llm_client.estimate_tokens(prompt_text="hi", max_tokens=100)
    long = llm_client.estimate_tokens(prompt_text="x" * 4000, max_tokens=100)
    assert long > short
    assert short >= 100  # at least max_tokens for output cap


# ---------------------------------------------------------------------------
# record_outcome — caller-initiated EMA adjustment
# ---------------------------------------------------------------------------


def test_record_outcome_flips_success_to_failure(tmp_path, monkeypatch):
    """Caller can override the auto-recorded 'success' if validation fails."""
    store = _store(tmp_path, ema_alpha=0.5)
    _patch_httpx(monkeypatch, body=_ok_payload())
    llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        store=store,
    )
    # Auto-recorded success
    assert store.snapshot("prospecting").efficiency_ema == pytest.approx(1.0)
    # Caller validates and decides it was a failure
    llm_client.record_outcome("prospecting", outcome="failure", store=store)
    b = store.snapshot("prospecting")
    # success + failure both counted on success_count and failure_count
    assert b.success_count_today == 1
    assert b.failure_count_today == 1
    # EMA after success then failure with alpha=0.5: 1 -> 1 -> 0.5
    assert b.efficiency_ema == pytest.approx(0.5)


def test_record_outcome_rejects_unknown():
    with pytest.raises(ValueError):
        llm_client.record_outcome("prospecting", outcome="bogus")
