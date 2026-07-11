"""Model floor enforcement (Control B, token-cost-hardening 2026-05-18).

When using the OpenAI backend (``OPENAI_API_KEY`` set) and
``allow_expensive_model=False`` (default), expensive models (gpt-4.1 full,
o-series) must raise :class:`ModelNotPermitted` before any HTTP call.

When using LM Studio (no ``OPENAI_API_KEY``), the model floor is a
pass-through — any locally loaded model is fine.
"""

from __future__ import annotations

import json

import pytest

from backend.common import llm_client
from backend.common.llm_budget import LlmBudgetStore
from backend.common.llm_global_budget import LlmGlobalBudgetStore


def _stores(tmp_path):
    s = LlmBudgetStore(
        base_token_budget=10_000,
        ema_alpha=0.5,
        floor_pct=0.10,
        ddb_table=None,
        json_path=str(tmp_path / "b.json"),
    )
    g = LlmGlobalBudgetStore(
        daily_dollar_cap=25.0,
        ddb_table=None,
        json_path=str(tmp_path / "g.json"),
    )
    return s, g


def _patch_httpx_to_assert_no_call(monkeypatch):
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
            raise AssertionError("HTTP must not be called when model floor blocks")

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    return called


def _patch_httpx_ok(monkeypatch, *, in_tokens: int = 100, out_tokens: int = 50):
    body = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens},
    }

    class _Resp:
        status_code = 200
        text = json.dumps(body)

        def json(self):
            return body

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)


def _enable_openai_backend(monkeypatch):
    """Simulate the OpenAI backend being active for model floor tests."""
    monkeypatch.setattr(llm_client, "_USING_OPENAI", True)


# ---------------------------------------------------------------------------
# Floor blocks expensive OpenAI models by default
# ---------------------------------------------------------------------------


def test_gpt41_call_without_opt_in_raises(tmp_path, monkeypatch):
    _enable_openai_backend(monkeypatch)
    s, g = _stores(tmp_path)
    called = _patch_httpx_to_assert_no_call(monkeypatch)
    with pytest.raises(llm_client.ModelNotPermitted):
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="hi",
            model="gpt-4.1",
            store=s,
            global_store=g,
        )
    assert called["n"] == 0


def test_o3_call_without_opt_in_raises(tmp_path, monkeypatch):
    _enable_openai_backend(monkeypatch)
    s, g = _stores(tmp_path)
    _patch_httpx_to_assert_no_call(monkeypatch)
    with pytest.raises(llm_client.ModelNotPermitted):
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="hi",
            model="o3",
            store=s,
            global_store=g,
        )


# ---------------------------------------------------------------------------
# Cheap models pass the floor by default
# ---------------------------------------------------------------------------


def test_gpt41_mini_passes_floor(tmp_path, monkeypatch):
    _enable_openai_backend(monkeypatch)
    s, g = _stores(tmp_path)
    _patch_httpx_ok(monkeypatch)
    text, _ = llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        model="gpt-4.1-mini",
        store=s,
        global_store=g,
    )
    assert text == "ok"


# ---------------------------------------------------------------------------
# LM Studio backend passes all models through
# ---------------------------------------------------------------------------


def test_lm_studio_any_model_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "_USING_OPENAI", False)
    s, g = _stores(tmp_path)
    _patch_httpx_ok(monkeypatch)
    text, _ = llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        model="claude-opus-4-20250514",
        store=s,
        global_store=g,
    )
    assert text == "ok"


# ---------------------------------------------------------------------------
# Explicit opt-in lets expensive models through
# ---------------------------------------------------------------------------


def test_gpt41_call_with_opt_in_proceeds(tmp_path, monkeypatch):
    _enable_openai_backend(monkeypatch)
    s, g = _stores(tmp_path)
    _patch_httpx_ok(monkeypatch)
    text, _ = llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        model="gpt-4.1",
        allow_expensive_model=True,
        store=s,
        global_store=g,
    )
    assert text == "ok"


# ---------------------------------------------------------------------------
# Floor exception carries the model name
# ---------------------------------------------------------------------------


def test_floor_exception_message_includes_model(tmp_path, monkeypatch):
    _enable_openai_backend(monkeypatch)
    s, g = _stores(tmp_path)
    _patch_httpx_to_assert_no_call(monkeypatch)
    with pytest.raises(llm_client.ModelNotPermitted) as ei:
        llm_client.anthropic_messages(
            workcell="prospecting",
            api_key="k",
            prompt="hi",
            model="gpt-4.1",
            store=s,
            global_store=g,
        )
    msg = str(ei.value)
    assert "gpt-4.1" in msg
    assert "allow_expensive_model" in msg
