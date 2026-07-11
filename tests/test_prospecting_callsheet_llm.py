"""Tests for the LLM-personalized callsheet path.

After the migration to ``backend.common.llm_client``, tests stub the
``anthropic_messages`` wrapper directly rather than monkeypatching httpx.
This keeps test fixtures focused on the callsheet's own logic
(prompt-building, JSON parsing, fallback selection, outcome-on-parse-fail)
without re-asserting wrapper internals already covered by
``test_llm_client.py``.

Origin-branch tests (raw-httpx approach) are retained as ``_origin``
suffixed variants where the test name overlaps with the wrapper-approach
tests, so both coverage paths survive.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock


from backend.common import llm_client
from backend.prospecting import callsheet as cs
from backend.prospecting.models import ProspectRecord


def _prospect(**overrides) -> ProspectRecord:
    base = dict(
        company_name="Acme Roofing",
        industry="roofing",
        city="Yuba City",
        zipcode="95993",
        business_categories="roofing_contractor",
        review_rating="4.6",
        review_count="38",
        call_priority="hot",
        # lead_score>=75 needed to satisfy build_call_sheet_smart's top-N gate.
        # See project_samus_llm_token_policy (Lever 2.2).
        lead_score=80,
    )
    base.update(overrides)
    return ProspectRecord(**base)


# ---------------------------------------------------------------------------
# Origin-branch helper: raw httpx stub
# ---------------------------------------------------------------------------


def _stub_client_with_response(monkeypatch, response_payload, status_code=200):
    fake_response = MagicMock()
    fake_response.status_code = status_code
    fake_response.json = MagicMock(return_value=response_payload)
    fake_response.raise_for_status = MagicMock()

    class _FakeClient:
        last_url = None
        last_headers = None
        last_json = None

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def post(self, url, *, headers=None, json=None):
            _FakeClient.last_url = url
            _FakeClient.last_headers = headers
            _FakeClient.last_json = json
            return fake_response

    monkeypatch.setattr(cs.httpx, "Client", _FakeClient)
    return _FakeClient


# ---------------------------------------------------------------------------
# HEAD-branch helper: llm_client wrapper stub
# ---------------------------------------------------------------------------


def _stub_wrapper(
    monkeypatch,
    *,
    text: str | None = None,
    raise_exc: Exception | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Replace cs.anthropic_messages with a recording stub.

    Returns a dict that gets populated with the call's kwargs so tests can
    assert on the prompt / workcell / max_tokens that were sent.
    """
    captured: dict[str, Any] = {}

    def _fake(*, workcell, api_key, prompt, max_tokens=1024, **kwargs):
        captured["workcell"] = workcell
        captured["api_key"] = api_key
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        if raise_exc is not None:
            raise raise_exc
        return (text or "", usage or {"input_tokens": 100, "output_tokens": 50})

    monkeypatch.setattr(cs, "anthropic_messages", _fake)
    return captured


def _stub_record_outcome(monkeypatch) -> list[dict[str, Any]]:
    """Replace cs.record_outcome with a list-appending stub."""
    calls: list[dict[str, Any]] = []

    def _fake(workcell, *, outcome, store=None):
        calls.append({"workcell": workcell, "outcome": outcome})

    monkeypatch.setattr(cs, "record_outcome", _fake)
    return calls


# ---------------------------------------------------------------------------
# Fallback when no key (both approaches agree on this behaviour)
# ---------------------------------------------------------------------------


def test_build_call_sheet_with_llm_falls_back_when_key_empty(monkeypatch):
    p = _prospect()
    out = cs.build_call_sheet_with_llm(p, anthropic_api_key="")
    assert "Acme Roofing" in out.callsheet_opener
    assert out.callsheet_pitch  # populated by template


# ---------------------------------------------------------------------------
# LLM success path — wrapper approach (HEAD branch)
# ---------------------------------------------------------------------------


def test_build_call_sheet_with_llm_sends_expected_prompt(monkeypatch):
    payload = json.dumps(
        {
            "pitch": "Local rankings drive 80% of inbound roofing calls.",
            "opener": "Hi, this is [NAME] with HustleForge — I noticed Acme Roofing in Yuba City.",
            "voicemail": "Hi, this is [NAME] from HustleForge. Call [PHONE] back.",
        }
    )
    captured = _stub_wrapper(monkeypatch, text=payload)

    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="sk-test-key")

    assert captured["workcell"] == "prospecting"
    assert captured["api_key"] == "unused"
    assert captured["max_tokens"] == 350
    # Prompt carries the prospect identity so the model can personalize.
    assert "Acme Roofing" in captured["prompt"]
    assert "Yuba City" in captured["prompt"]

    assert "Local rankings" in out.callsheet_pitch
    assert "Acme Roofing" in out.callsheet_opener
    assert "[PHONE]" in out.callsheet_voicemail
    # offer/issues/objections still come from templates
    assert out.callsheet_offer
    assert out.callsheet_issues
    assert out.callsheet_objections


# ---------------------------------------------------------------------------
# LLM success path — raw-httpx approach (origin branch)
# Retained as _origin variant: different stub shape, verifies wire format
# ---------------------------------------------------------------------------


def test_build_call_sheet_with_llm_routes_through_llm_client(monkeypatch):
    """Verify callsheet LLM path goes through anthropic_messages (now LM Studio)."""
    llm_response = json.dumps(
        {
            "pitch": "Local rankings drive 80% of inbound roofing calls.",
            "opener": "Hi, this is [NAME] with HustleForge — I noticed Acme Roofing in Yuba City.",
            "voicemail": "Hi, this is [NAME] from HustleForge. Call [PHONE] back.",
        }
    )
    monkeypatch.setattr(
        cs,
        "anthropic_messages",
        lambda **kw: (
            llm_response,
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        ),
    )
    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="unused")

    assert "Local rankings" in out.callsheet_pitch
    assert "Acme Roofing" in out.callsheet_opener
    assert "[PHONE]" in out.callsheet_voicemail
    assert out.callsheet_offer
    assert out.callsheet_issues
    assert out.callsheet_objections


# ---------------------------------------------------------------------------
# Wrapper-side errors -> fall back to template, no outcome flip (HEAD)
# ---------------------------------------------------------------------------


def test_falls_back_on_llm_call_error_without_failure_flip(monkeypatch):
    """Transport / 5xx: wrapper records outcome=error itself. Caller MUST NOT
    additionally call record_outcome('failure') — that would double-count."""
    _stub_wrapper(monkeypatch, raise_exc=llm_client.LlmCallError("transport down"))
    flips = _stub_record_outcome(monkeypatch)
    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="sk-test")
    assert "Acme Roofing" in out.callsheet_opener  # template fallback
    assert flips == []  # no caller-side outcome record


def test_falls_back_on_budget_exceeded(monkeypatch):
    """Pre-flight quota deny: wrapper raises BudgetExceeded; caller falls back."""
    from backend.common.llm_budget import QuotaDecision

    decision = QuotaDecision(
        allowed=False, quota=100, used=200, requested=50, reason="budget_exceeded: ..."
    )
    _stub_wrapper(monkeypatch, raise_exc=llm_client.BudgetExceeded(decision))
    flips = _stub_record_outcome(monkeypatch)
    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="sk-test")
    assert "Acme Roofing" in out.callsheet_opener
    assert flips == []


# ---------------------------------------------------------------------------
# Transport error — wrapper approach (replaces origin-branch httpx stub)
# ---------------------------------------------------------------------------


def test_build_call_sheet_with_llm_falls_back_on_transport_error(monkeypatch):
    """LlmCallError triggers template fallback via the wrapper path."""
    _stub_wrapper(monkeypatch, raise_exc=llm_client.LlmCallError("dns failure"))

    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="unused")
    assert "Acme Roofing" in out.callsheet_opener


# ---------------------------------------------------------------------------
# Parse-time failures -> caller flips outcome to "failure" (HEAD)
# ---------------------------------------------------------------------------


def test_falls_back_on_invalid_json_and_flips_outcome(monkeypatch):
    """Model returned 200 but content unparseable: tokens burned, no value.
    Caller MUST flip the wrapper's auto-success to outcome=failure so the
    workcell's EMA reflects the wasted spend."""
    _stub_wrapper(monkeypatch, text="not json at all")
    flips = _stub_record_outcome(monkeypatch)
    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="sk-test")
    assert "Acme Roofing" in out.callsheet_opener  # template fallback
    assert len(flips) == 1
    assert flips[0]["workcell"] == "prospecting"
    assert flips[0]["outcome"] == "failure"


def test_falls_back_on_missing_required_field_and_flips_outcome(monkeypatch):
    """Model returned JSON but a required field is empty -> failure."""
    payload = json.dumps({"pitch": "p", "opener": "", "voicemail": "v"})
    _stub_wrapper(monkeypatch, text=payload)
    flips = _stub_record_outcome(monkeypatch)
    cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="sk-test")
    assert len(flips) == 1
    assert flips[0]["outcome"] == "failure"


# ---------------------------------------------------------------------------
# Invalid JSON — wrapper approach (replaces origin-branch httpx stub)
# ---------------------------------------------------------------------------


def test_build_call_sheet_with_llm_falls_back_on_invalid_json_wrapper(monkeypatch):
    """Model returns unparseable text via the wrapper path -> template fallback."""
    _stub_wrapper(monkeypatch, text="not json at all")
    _stub_record_outcome(monkeypatch)

    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="unused")
    assert "Acme Roofing" in out.callsheet_opener


# ---------------------------------------------------------------------------
# Code-fenced response (tolerated) — both approaches agree
# ---------------------------------------------------------------------------


def test_build_call_sheet_with_llm_strips_code_fence(monkeypatch):
    payload = (
        "```json\n"
        + json.dumps(
            {
                "pitch": "p",
                "opener": "o",
                "voicemail": "v",
            }
        )
        + "\n```"
    )
    _stub_wrapper(monkeypatch, text=payload)
    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="sk-test")
    assert out.callsheet_pitch == "p"
    assert out.callsheet_opener == "o"
    assert out.callsheet_voicemail == "v"


def test_build_call_sheet_with_llm_strips_code_fence_wrapper(monkeypatch):
    """Code-fence tolerance via the wrapper path."""
    payload = "```json\n" + json.dumps({"pitch": "p", "opener": "o", "voicemail": "v"}) + "\n```"
    _stub_wrapper(monkeypatch, text=payload)

    out = cs.build_call_sheet_with_llm(_prospect(), anthropic_api_key="unused")
    assert out.callsheet_pitch == "p"
    assert out.callsheet_opener == "o"
    assert out.callsheet_voicemail == "v"


# ---------------------------------------------------------------------------
# build_call_sheet_smart: settings-driven selection
# ---------------------------------------------------------------------------


def test_build_call_sheet_smart_uses_template_without_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from backend.common.settings import reload_settings

    reload_settings()
    out = cs.build_call_sheet_smart(_prospect())
    assert "Acme Roofing" in out.callsheet_opener


def test_build_call_sheet_smart_calls_llm_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    from backend.common.settings import reload_settings

    reload_settings()
    payload = json.dumps(
        {
            "pitch": "Custom-pitch",
            "opener": "Custom-opener",
            "voicemail": "Custom-voicemail",
        }
    )
    _stub_wrapper(monkeypatch, text=payload)
    out = cs.build_call_sheet_smart(_prospect())
    assert out.callsheet_pitch == "Custom-pitch"
    assert out.callsheet_opener == "Custom-opener"
    assert out.callsheet_voicemail == "Custom-voicemail"


def test_build_call_sheet_smart_calls_llm_wrapper(monkeypatch):
    """Wrapper path: smart selector routes hot prospects through the LLM."""
    payload = json.dumps(
        {
            "pitch": "Custom-pitch",
            "opener": "Custom-opener",
            "voicemail": "Custom-voicemail",
        }
    )
    _stub_wrapper(monkeypatch, text=payload)

    out = cs.build_call_sheet_smart(_prospect())
    assert out.callsheet_pitch == "Custom-pitch"
    assert out.callsheet_opener == "Custom-opener"
    assert out.callsheet_voicemail == "Custom-voicemail"
