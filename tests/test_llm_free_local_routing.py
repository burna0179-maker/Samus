"""Free-local LLM routing (2026-07-08) — a free-listed workcell runs on LM
Studio UNMETERED, bypassing the paid $-cap + per-workcell token quota.

Motivation: cognition exhausted its 200k paid token quota and got DENIED on
work that could run free on local LM Studio. A free-routed call must never
touch the budget stores.
"""

from __future__ import annotations

import backend.common.llm_client as lc


class _FakeResp:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": "free local answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    @property
    def text(self):
        return ""

    @property
    def content(self):
        return b'{"ok":1}'

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self):
        self.posted_url = None

    def post(self, url, headers=None, json=None):
        self.posted_url = url
        self.body = json
        return _FakeResp()


def _boom_store(*a, **k):  # any budget-store touch fails the test
    raise AssertionError("free-local call must NOT touch the budget store")


def test_free_workcell_env_parsing(monkeypatch):
    monkeypatch.setenv("SAMUS_LLM_FREE_WORKCELLS", "cognition, efh_semantic ,")
    assert lc._is_free_workcell("cognition")
    assert lc._is_free_workcell("efh_semantic")
    assert not lc._is_free_workcell("outreach")


def test_lm_studio_url_normalisation(monkeypatch):
    monkeypatch.setenv("SAMUS_LM_STUDIO_URL", "http://host.docker.internal:1234/v1")
    assert lc._lm_studio_completions_url() == "http://host.docker.internal:1234/v1/chat/completions"
    monkeypatch.setenv("SAMUS_LM_STUDIO_URL", "http://x:1234/v1/chat/completions")
    assert lc._lm_studio_completions_url() == "http://x:1234/v1/chat/completions"


def test_free_routed_call_bypasses_budget(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setenv("SAMUS_LLM_FREE_WORKCELLS", "cognition")
    monkeypatch.setenv("SAMUS_LM_STUDIO_URL", "http://lm:1234/v1")
    monkeypatch.setattr(lc, "get_shared_client", lambda timeout=None: fake)

    # If the budget gate were consulted, these would raise.
    class _Boom:
        can_spend = staticmethod(_boom_store)
        can_spend_global = staticmethod(_boom_store)
        record_spend = staticmethod(_boom_store)

    monkeypatch.setattr(lc, "get_store", lambda: _Boom())
    monkeypatch.setattr(lc, "get_global_store", lambda: _Boom())

    text, usage = lc.anthropic_messages(
        workcell="cognition",
        api_key="",
        prompt="hi",
    )
    assert text == "free local answer"
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 5
    # It POSTed to the LM Studio completions endpoint, not OpenAI.
    assert fake.posted_url == "http://lm:1234/v1/chat/completions"
    assert fake.body["model"] == "local"


def test_prefer_local_routes_free_for_any_workcell(monkeypatch):
    fake = _FakeClient()
    monkeypatch.delenv("SAMUS_LLM_FREE_WORKCELLS", raising=False)
    monkeypatch.setenv("SAMUS_LM_STUDIO_URL", "http://lm:1234/v1")
    monkeypatch.setattr(lc, "get_shared_client", lambda timeout=None: fake)

    class _Boom:
        can_spend = staticmethod(_boom_store)
        can_spend_global = staticmethod(_boom_store)
        record_spend = staticmethod(_boom_store)

    monkeypatch.setattr(lc, "get_store", lambda: _Boom())
    monkeypatch.setattr(lc, "get_global_store", lambda: _Boom())

    text, _ = lc.anthropic_messages(
        workcell="anything",
        api_key="",
        prompt="hi",
        prefer_local=True,
    )
    assert text == "free local answer"
    assert fake.posted_url == "http://lm:1234/v1/chat/completions"
