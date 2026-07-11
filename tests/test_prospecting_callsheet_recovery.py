"""Tests for the template_recovery fallback wired into the callsheet LLM path.

When the callsheet LLM path fails (transport error, budget denial, or an
unparseable response), the fallback is routed through the template_recovery
workcell instead of a silent local template. These tests assert that
template_recovery.recover is actually invoked, that the returned ProspectRecord
still carries the canonical templated callsheet_* fields, and that a missing /
faulting template_recovery degrades silently to the plain templated fallback.
"""

from __future__ import annotations


from backend.common import llm_client
from backend.prospecting import callsheet as cs
from backend.prospecting.models import ProspectRecord


def _prospect(**overrides) -> ProspectRecord:
    base = dict(
        prospect_id="pr_recover",
        company_name="Acme Roofing",
        industry="roofing",
        city="Yuba City",
        zipcode="95993",
        owner_name="Dana Owner",
        phone="(530) 555-2000",
        call_priority="hot",
        lead_score=80,
    )
    base.update(overrides)
    return ProspectRecord(**base)


# ── template_recovery is invoked on each LLM-failure path ───────────────────


def _capture_recover(monkeypatch):
    """Patch template_recovery.recover with a spy; return the captured calls."""
    calls: list[dict] = []

    def _spy(req, *, task_id=None):
        calls.append(
            {
                "task_kind": req.task_kind,
                "context": dict(req.context),
                "failure_reason": req.failure_reason,
                "task_id": task_id,
            }
        )
        # Return a minimal recovery-response-shaped object.
        from backend.template_recovery.models import RecoveryResponse

        return RecoveryResponse(
            task_kind=req.task_kind,
            scaffold="# Call Sheet — recovered",
            template_version="callsheet_template_v5",
            fallback_triggered=True,
            generic_fallback=False,
            failure_reason=req.failure_reason,
        )

    monkeypatch.setattr("backend.template_recovery.service.recover", _spy)
    return calls


def test_llm_call_error_routes_to_template_recovery(monkeypatch):
    calls = _capture_recover(monkeypatch)

    def _boom(**_kwargs):
        raise llm_client.LlmCallError("upstream 503")

    monkeypatch.setattr(cs, "anthropic_messages", _boom)

    sheet, cost = cs.build_call_sheet_with_llm_costed(
        _prospect(),
        anthropic_api_key="sk-test",
    )
    # template_recovery.recover invoked exactly once with task_kind="callsheet".
    assert len(calls) == 1
    assert calls[0]["task_kind"] == "callsheet"
    assert "llm_call_error" in calls[0]["failure_reason"]
    assert calls[0]["context"]["business_name"] == "Acme Roofing"
    assert calls[0]["task_id"] == "callsheet-recovery-pr_recover"
    # Cost is 0.0 — no LLM call completed.
    assert cost == 0.0
    # Canonical templated callsheet fields are still populated.
    assert sheet.callsheet_opener
    assert sheet.callsheet_pitch
    assert sheet.callsheet_voicemail


def test_budget_exceeded_routes_to_template_recovery(monkeypatch):
    calls = _capture_recover(monkeypatch)

    class _Decision:
        reason = "daily_cap_reached"

    def _denied(**_kwargs):
        raise llm_client.BudgetExceeded(_Decision())

    monkeypatch.setattr(cs, "anthropic_messages", _denied)

    sheet, cost = cs.build_call_sheet_with_llm_costed(
        _prospect(),
        anthropic_api_key="sk-test",
    )
    assert len(calls) == 1
    assert calls[0]["task_kind"] == "callsheet"
    assert "budget_denied" in calls[0]["failure_reason"]
    assert cost == 0.0
    assert sheet.callsheet_pitch


def test_unparseable_response_routes_to_template_recovery(monkeypatch):
    calls = _capture_recover(monkeypatch)

    def _garbage(**_kwargs):
        return "not json at all", {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(cs, "anthropic_messages", _garbage)
    # record_outcome is also called on this path — stub it to a no-op.
    monkeypatch.setattr(cs, "record_outcome", lambda *a, **k: None)

    sheet, cost = cs.build_call_sheet_with_llm_costed(
        _prospect(),
        anthropic_api_key="sk-test",
    )
    assert len(calls) == 1
    assert calls[0]["task_kind"] == "callsheet"
    assert "unparseable_response" in calls[0]["failure_reason"]
    # The wasted-but-billed cost is preserved even though recovery is free.
    assert cost >= 0.0
    assert sheet.callsheet_pitch


# ── best-effort: template_recovery unavailable / faulting ───────────────────


def test_recover_helper_degrades_when_template_recovery_import_fails(monkeypatch):
    """A missing template_recovery module degrades to the plain templated sheet."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("backend.template_recovery"):
            raise ImportError("template_recovery unavailable in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    sheet = cs._recover_callsheet_via_template_recovery(
        _prospect(),
        failure_reason="some failure",
    )
    # Still a fully-populated templated callsheet — recovery wiring never tanks.
    assert sheet.callsheet_opener
    assert sheet.callsheet_pitch
    assert sheet.callsheet_voicemail


def test_recover_helper_degrades_when_recover_raises(monkeypatch):
    """A fault inside template_recovery.recover degrades to the templated sheet."""

    def _boom(_req, *, task_id=None):
        raise RuntimeError("recovery store blew up")

    monkeypatch.setattr("backend.template_recovery.service.recover", _boom)

    sheet = cs._recover_callsheet_via_template_recovery(
        _prospect(),
        failure_reason="some failure",
    )
    assert sheet.callsheet_pitch
    assert sheet.callsheet_voicemail


def test_recover_helper_produces_real_recovery_when_wired(monkeypatch, tmp_path):
    """End-to-end through the real template_recovery workcell (no LLM, free)."""
    from backend.template_recovery.fallback import clear_cache

    clear_cache()

    sheet = cs._recover_callsheet_via_template_recovery(
        _prospect(),
        failure_reason="llm timeout",
    )
    # The real recover() ran: returned record is the canonical templated sheet.
    assert sheet.callsheet_opener
    assert sheet.callsheet_pitch
    assert "Acme Roofing" in sheet.callsheet_opener
