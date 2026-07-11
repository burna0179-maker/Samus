"""Prompt-caching plumbing (Control D, token-cost-hardening 2026-05-18).

With the switch to OpenAI Chat Completions API, ``cache_system`` is
accepted for backward compat but is a no-op — neither OpenAI nor
LM Studio uses the Anthropic prompt-caching wire format. These tests
verify the parameter is silently accepted and the usage dict still
surfaces cache fields (defaulting to 0, since neither backend populates
them).
"""

from __future__ import annotations

import json


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


def _ok_response(prompt_tokens=5, completion_tokens=3, text="ok"):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _capture_httpx(monkeypatch, *, response_body: dict):
    captured = {"headers": None, "body": None}

    class _Resp:
        status_code = 200
        text = json.dumps(response_body)

        def json(self):
            return response_body

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["headers"] = headers
            captured["body"] = json
            return _Resp()

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    return captured


# ---------------------------------------------------------------------------
# cache_system is a no-op — system is a plain string, no beta header
# ---------------------------------------------------------------------------


def test_cache_system_true_still_works(tmp_path, monkeypatch):
    """cache_system=True is accepted but doesn't change the wire format."""
    s, g = _stores(tmp_path)
    cap = _capture_httpx(monkeypatch, response_body=_ok_response())
    llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        system="you are a helpful assistant",
        cache_system=True,
        store=s,
        global_store=g,
    )
    body = cap["body"]
    messages = body["messages"]
    sys_msg = [m for m in messages if m["role"] == "system"]
    assert len(sys_msg) == 1
    assert sys_msg[0]["content"] == "you are a helpful assistant"


def test_cache_system_false_same_as_true(tmp_path, monkeypatch):
    """Both values produce the same OpenAI messages format."""
    s, g = _stores(tmp_path)
    cap = _capture_httpx(monkeypatch, response_body=_ok_response())
    llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        system="sys",
        store=s,
        global_store=g,
    )
    body = cap["body"]
    messages = body["messages"]
    sys_msg = [m for m in messages if m["role"] == "system"]
    assert len(sys_msg) == 1
    assert sys_msg[0]["content"] == "sys"


# ---------------------------------------------------------------------------
# Response handling — cache counters default to 0
# ---------------------------------------------------------------------------


def test_missing_cache_fields_default_zero(tmp_path, monkeypatch):
    """Usage dict always includes cache fields, defaulting to 0."""
    s, g = _stores(tmp_path)
    _capture_httpx(monkeypatch, response_body=_ok_response())
    _, usage = llm_client.anthropic_messages(
        workcell="prospecting",
        api_key="k",
        prompt="hi",
        store=s,
        global_store=g,
    )
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0
