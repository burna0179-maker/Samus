"""local_llm endpoint resolution — tolerate base/full/bare URL forms (Gap-11).

The 6/30 outage: SAMUS_LM_STUDIO_URL was set to the FULL …/v1/chat/completions
URL, but chat() blindly appended /chat/completions → a doubled path that LM
Studio answered with HTTP 200 + empty choices, silently dark-ing the whole
offline reasoning stack (session monitor, strategy reasoner, callsheet updater,
pattern aggregator). The resolver must normalise every reasonable form.
"""

from __future__ import annotations

from backend.common.local_llm import _resolve_chat_endpoint


def test_base_v1_form_appends_path():
    assert (
        _resolve_chat_endpoint("http://host.docker.internal:1234/v1")
        == "http://host.docker.internal:1234/v1/chat/completions"
    )


def test_full_url_is_left_intact_not_doubled():
    # The bug: this used to become …/chat/completions/chat/completions.
    assert (
        _resolve_chat_endpoint("http://host.docker.internal:1234/v1/chat/completions")
        == "http://host.docker.internal:1234/v1/chat/completions"
    )


def test_trailing_slash_tolerated():
    assert (
        _resolve_chat_endpoint("http://host.docker.internal:1234/v1/")
        == "http://host.docker.internal:1234/v1/chat/completions"
    )
    assert (
        _resolve_chat_endpoint("http://host.docker.internal:1234/v1/chat/completions/")
        == "http://host.docker.internal:1234/v1/chat/completions"
    )


def test_bare_host_port_gets_v1_path():
    assert (
        _resolve_chat_endpoint("http://host.docker.internal:1234")
        == "http://host.docker.internal:1234/v1/chat/completions"
    )


def test_openai_base_form():
    assert (
        _resolve_chat_endpoint("https://api.openai.com/v1")
        == "https://api.openai.com/v1/chat/completions"
    )


# ---------------------------------------------------------------------------
# Backend fallback: local primary, OpenAI backup
# ---------------------------------------------------------------------------

import backend.common.local_llm as llm


def test_backend_order_local_primary_no_openai(monkeypatch):
    monkeypatch.setattr(llm, "_PRIMARY", "local")
    monkeypatch.setattr(llm, "_OPENAI_API_KEY", None)
    assert llm._backend_order() == ["local"]


def test_backend_order_local_primary_with_openai(monkeypatch):
    monkeypatch.setattr(llm, "_PRIMARY", "local")
    monkeypatch.setattr(llm, "_OPENAI_API_KEY", "sk-test")
    assert llm._backend_order() == ["local", "openai"]


def test_backend_order_openai_primary(monkeypatch):
    monkeypatch.setattr(llm, "_PRIMARY", "openai")
    monkeypatch.setattr(llm, "_OPENAI_API_KEY", "sk-test")
    assert llm._backend_order() == ["openai", "local"]


def test_chat_falls_back_to_openai_when_local_empty(monkeypatch):
    """Local returns empty (the 6/30 failure mode) → OpenAI serves the result."""
    monkeypatch.setattr(llm, "_PRIMARY", "local")
    monkeypatch.setattr(llm, "_OPENAI_API_KEY", "sk-test")
    calls = []

    def fake_backend(*, kind, system, user, max_tokens, temperature, timeout):
        calls.append(kind)
        return "" if kind == "local" else "OPENAI ANSWER"

    monkeypatch.setattr(llm, "_call_backend", fake_backend)

    out = llm.chat("sys", "user")
    assert out == "OPENAI ANSWER"
    assert calls == ["local", "openai"]  # tried local first, then fell back


def test_chat_uses_local_when_it_succeeds(monkeypatch):
    """Local works → OpenAI is never called (no cost)."""
    monkeypatch.setattr(llm, "_PRIMARY", "local")
    monkeypatch.setattr(llm, "_OPENAI_API_KEY", "sk-test")
    calls = []

    def fake_backend(*, kind, system, user, max_tokens, temperature, timeout):
        calls.append(kind)
        return "LOCAL ANSWER"

    monkeypatch.setattr(llm, "_call_backend", fake_backend)

    out = llm.chat("sys", "user")
    assert out == "LOCAL ANSWER"
    assert calls == ["local"]  # OpenAI never reached


def test_chat_empty_when_all_backends_fail(monkeypatch):
    monkeypatch.setattr(llm, "_PRIMARY", "local")
    monkeypatch.setattr(llm, "_OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_call_backend", lambda **kw: "")
    assert llm.chat("sys", "user") == ""
